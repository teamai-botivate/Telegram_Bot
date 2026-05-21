from __future__ import annotations

import asyncio
import json
import os
import re
from typing import Any

from app.platforms.base import BotMessage, Platform, send_reply
from app.models import RegisteredClient, Tenant

from .core import (
    logger,
    OPENAI_API_KEY,
    DB_ROUTER_MODEL,
    ENABLE_QUERY_LEARNING,
    RETRIEVAL_FAILURE_MESSAGE,
    DATABASE_CONNECTION_MESSAGE,
    OFF_TOPIC_MESSAGE,
    pick_off_topic_reply,
)
from .llm import _get_openai_client, _call_openai_formatting, is_off_topic, _call_openai_classifier
from .intent import detect_intent
from .context import _build_conversation_context_block, _remember_conversation_context
from .schema import _extract_table_names_from_blueprint, _extract_sheet_value_filters, _validate_generated_sql, _fix_unsupported_postgres_constructs, _maybe_expand_count_query_across_tables
from .sqlgen import generate_sql_query, detect_multi_table_query, fix_sql
from .format import format_sql_response
from .smart_format import smart_format_response
from app.database import TenantDBConnectionError, QueryExecutionError, SecurityError, fetch_credential_postgres_runtime_schema, execute_credential_query, store_query_example, get_tenant_credentials, get_tenant_credentials_all, get_tenant_by_chat_id, find_registered_client_by_chat

def _summarize_credential_for_router(credential: Any) -> dict[str, Any]:
    """Build the compact per-DB summary the router LLM sees."""
    table_names: list[str] = []
    blueprint = getattr(credential, "schema_blueprint", None)
    if blueprint:
        table_names = _extract_table_names_from_blueprint(blueprint)
    cred_id = str(getattr(credential, "id", "credential"))
    slug = getattr(credential, "product_slug", None) or cred_id
    display = getattr(credential, "display_name", None) or slug or "Database"
    return {
        "product_slug": slug,
        "display_name": display,
        "db_type": (getattr(credential, "db_type", "") or "").lower(),
        "table_names": table_names[:25],  # cap to keep prompt small
    }

async def route_question_to_database(
    tenant_id: Any, question: str
) -> list[Any]:
    """Pick which of the tenant's databases should answer the question.

    - 0 connections → returns [].
    - 1 connection → returns it. No LLM call.
    - 2+ connections → asks gpt-4o-mini for the slug(s); falls back to all DBs on failure.
    """
    credentials = await get_tenant_credentials_all(tenant_id)

    if not credentials:
        return []

    if len(credentials) == 1:
        # Fast path: zero added latency, zero LLM tokens, no behavior change for
        # single-DB tenants.
        return credentials

    summaries = [_summarize_credential_for_router(c) for c in credentials]
    slug_to_credential: dict[str, Any] = {}
    for cred, summary in zip(credentials, summaries):
        slug_to_credential[summary["product_slug"]] = cred

    user_prompt = (
        f"User question: {question}\n\n"
        f"Available databases for this tenant:\n"
        f"{json.dumps(summaries, indent=2)}\n\n"
        "Which database(s) are needed to answer this question? Return JSON only:\n"
        '{"databases": ["product_slug1", "product_slug2"], "reason": "..."}\n'
        "- Return 1 slug if the question clearly fits one database.\n"
        "- Return 2+ slugs ONLY if the question explicitly spans multiple databases.\n"
        "- If unsure, return the single best match."
    )

    chosen_slugs: list[str] = []
    reason = ""
    try:
        from .llm import _get_fast_llm_client
        client = _get_fast_llm_client()
        completion = await client.chat.completions.create(
            model=DB_ROUTER_MODEL,
            temperature=0,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a database router. Given a user question and a list of "
                        "available databases, pick which database(s) should be queried. "
                        "Output ONLY valid JSON, nothing else."
                    ),
                },
                {"role": "user", "content": user_prompt},
            ],
        )
        raw = completion.choices[0].message.content or "{}"
        # Strip markdown code fences if the model wraps the JSON
        raw = re.sub(r"^```(?:json)?\s*", "", raw.strip())
        raw = re.sub(r"\s*```$", "", raw.strip())
        parsed = json.loads(raw)
        chosen_slugs = [str(s) for s in parsed.get("databases", []) if isinstance(s, str)]
        reason = str(parsed.get("reason", ""))[:300]
    except Exception as exc:
        logger.warning("[ROUTE] LLM router failed; falling back to all DBs: %s", exc)
        return credentials

    matched: list[Any] = []
    seen: set[str] = set()
    for slug in chosen_slugs:
        if slug in slug_to_credential and slug not in seen:
            matched.append(slug_to_credential[slug])
            seen.add(slug)

    if not matched:
        # LLM returned slugs that don't match any DB — treat as router failure.
        logger.warning(
            "[ROUTE] LLM returned no valid slugs (got %s); falling back to all DBs.",
            chosen_slugs,
        )
        return credentials

    logger.debug(
        "[ROUTE] tenant=%s dbs_available=%d dbs_chosen=%s reason=%r",
        tenant_id, len(credentials),
        [getattr(c, "product_slug", None) or str(getattr(c, "id", "?")) for c in matched],
        reason,
    )
    return matched

async def _build_welcome_message(chat_id: str) -> str:
    """Build a contextual welcome message showing what data is available."""
    try:
        tenant = await get_tenant_by_chat_id(chat_id)
        if tenant is None:
            return (
                "Hi! I'm Botivate Bot.\n\n"
                "I couldn't find your account yet. "
                "Please use your onboarding link to get started."
            )

        credentials = await get_tenant_credentials(tenant.id)
        if not credentials or not credentials.schema_blueprint:
            return (
                f"Hi! Welcome to {tenant.company_name}'s assistant.\n\n"
                "Your database isn't configured yet. "
                "Please contact your admin to complete setup."
            )

        # Extract table names from blueprint
        table_names = _extract_table_names_from_blueprint(credentials.schema_blueprint)
        tables_display = ", ".join(table_names) if table_names else "your business data"

        return (
            f"Hi! I'm {tenant.company_name}'s data assistant.\n\n"
            f"I can query: {tables_display}\n\n"
            "Try asking me:\n"
            "\u2022 How many pending tasks?\n"
            "\u2022 Show tasks assigned to [name]\n"
            "\u2022 What is [name]'s email?\n"
            "\u2022 Count records by department\n\n"
            "Type /help anytime for more examples."
        )
    except Exception:
        return "Hi! I'm ready. Ask me a business question and I'll fetch it from your data."

async def _run_postgres_pipeline_for_credential(
    msg: BotMessage, tenant: Any, credential: Any
) -> dict[str, Any]:
    """Run the SQL gen → execute → format pipeline against ONE Postgres credential.

    Returns a dict with keys:
      status: 'ok' | 'empty' | 'error' | 'connection_error'
      reply:  formatted answer string when status == 'ok'
      sql:    generated SQL string when status == 'ok'
    """
    conversation_context_block = _build_conversation_context_block(msg)
    metadata_blueprint = credential.schema_blueprint or "No semantic metadata available."
    cred_label = (
        getattr(credential, "display_name", None)
        or getattr(credential, "product_slug", None)
        or str(getattr(credential, "id", "credential"))
    )

    # ── Run schema fetch and question embedding concurrently ──
    from app.embeddings import embed_text

    async def _safe_embed() -> list[float] | None:
        try:
            return await embed_text(msg.text)
        except Exception as exc:
            logger.warning("[EMBED_PRE] failed: %s", exc)
            return None

    try:
        (runtime_schema, runtime_hints), question_embedding = await asyncio.gather(
            fetch_credential_postgres_runtime_schema(credential),
            _safe_embed(),
        )
    except TenantDBConnectionError as schema_error:
        logger.error("[SCHEMA_ERR] credential=%s error='%s'", credential.id, schema_error)
        return {"status": "connection_error"}

    blueprint = (
        "SEMANTIC METADATA (metadata_analysis.json):\n"
        f"{metadata_blueprint}\n\n"
        "TECHNICAL POSTGRESQL SCHEMA FOR SQL GENERATION:\n"
        f"{runtime_schema}"
    )
    auto_schema_hints = "\n".join(
        part
        for part in (getattr(credential, "auto_schema_hints", None), runtime_hints)
        if part and str(part).strip()
    )
    query_rows: list[dict[str, Any]] = []
    _generated_sql: str | None = None

    try:
        if detect_multi_table_query(msg.text):
            table_names = _extract_table_names_from_blueprint(blueprint)
            if not table_names:
                logger.info("[SQL_PIPELINE] No tables in blueprint for credential=%s", credential.id)
                return {"status": "empty"}

            combined_rows: list[dict[str, Any]] = []
            for table_name in table_names:
                table_sql = f"SELECT * FROM {table_name} LIMIT 2"
                logger.info(f"[SQL_OUT] {table_sql}")
                try:
                    rows = await execute_credential_query(
                        credential, table_sql, allow_select_star=True
                    )
                    for row in rows:
                        normalized = dict(row)
                        normalized["table_source"] = table_name
                        combined_rows.append(normalized)
                    logger.info(f"[SQL_OK] rows_returned={len(rows)}")
                except TenantDBConnectionError as e:
                    logger.error(f"[SQL_ERR] attempt=1 error='{e}'")
                    return {"status": "connection_error"}
                except (QueryExecutionError, SecurityError) as e:
                    logger.error(f"[SQL_ERR] attempt=1 error='{e}'")
            query_rows = combined_rows
        else:
            sql_query = await generate_sql_query(
                tenant.company_name,
                blueprint,
                msg.text,
                auto_schema_hints=auto_schema_hints,
                tenant_id=tenant.id,
                product_connection_id=None,
                conversation_context_block=conversation_context_block,
                precomputed_embedding=question_embedding,
            )
            sql_query = _maybe_expand_count_query_across_tables(sql_query, blueprint, msg.text)
            sql_query = _validate_generated_sql(sql_query)
            sql_query = await _fix_unsupported_postgres_constructs(sql_query, blueprint)
            logger.info(f"[SQL_OUT] {sql_query}")

            max_retries = 2
            attempt = 0
            while True:
                try:
                    query_rows = await execute_credential_query(credential, sql_query)
                    logger.info(f"[SQL_OK] rows_returned={len(query_rows)}")
                    _generated_sql = sql_query
                    break
                except TenantDBConnectionError as exec_error:
                    logger.error(f"[SQL_ERR] attempt={attempt + 1} error='{exec_error}'")
                    return {"status": "connection_error"}
                except (QueryExecutionError, SecurityError) as exec_error:
                    final_error = str(exec_error)
                    logger.error(f"[SQL_ERR] attempt={attempt + 1} error='{final_error}'")
                    if attempt >= max_retries:
                        logger.error(
                            f"[SQL_FAILED] credential={credential.id} question='{msg.text}' "
                            f"final_sql='{sql_query}' error='{final_error}'"
                        )
                        return {"status": "error"}
                    attempt += 1
                    sql_query = await fix_sql(sql_query, final_error, blueprint)
                    sql_query = _maybe_expand_count_query_across_tables(sql_query, blueprint, msg.text)
                    sql_query = _validate_generated_sql(sql_query)
                    sql_query = await _fix_unsupported_postgres_constructs(sql_query, blueprint)
                    logger.info(f"[SQL_OUT] {sql_query}")
    except Exception:
        logger.exception("[SQL_PIPELINE] Unhandled error for credential %s", credential.id)
        return {"status": "error"}

    if not query_rows:
        return {"status": "empty"}

    try:
        reply = await smart_format_response(tenant.company_name, msg.text, query_rows)
    except Exception as fmt_error:
        logger.error(f"[FORMAT_ERR] credential={credential.id} {fmt_error}")
        return {"status": "error"}

    if not reply:
        return {"status": "error"}

    _remember_conversation_context(msg, msg.text, reply, sql=_generated_sql)

    if ENABLE_QUERY_LEARNING and _generated_sql is not None and query_rows:
        async def _store_bg() -> None:
            try:
                await store_query_example(
                    tenant_id=tenant.id,
                    question=msg.text,
                    sql=_generated_sql,
                    product_connection_id=None,
                    verified_by="auto",
                )
            except Exception as store_error:
                logger.warning("[QUERY_LEARNING] Failed to store example for %s: %s", cred_label, store_error)
        asyncio.create_task(_store_bg())

    return {"status": "ok", "reply": reply, "sql": _generated_sql}

async def _run_sheets_pipeline_for_credential(
    msg: BotMessage, tenant: Any, credential: Any
) -> dict[str, Any]:
    """Run the Google Sheets answer pipeline against ONE Sheets credential."""
    from cryptography.fernet import InvalidToken
    from app.database import _decrypt_credential_value, fetch_google_sheet_runtime_context

    conversation_context_block = _build_conversation_context_block(msg)
    cred_label = (
        getattr(credential, "display_name", None)
        or getattr(credential, "product_slug", None)
        or str(getattr(credential, "id", "credential"))
    )

    try:
        decrypted_url = _decrypt_credential_value(credential.connection_url)
        sheet_id = decrypted_url.replace("google_sheets://", "")
        creds_json = (
            _decrypt_credential_value(credential.google_credentials)
            if credential.google_credentials
            else None
        )
    except (InvalidToken, Exception):
        logger.error("[SHEETS_DECRYPT_ERR] credential=%s", credential.id)
        return {"status": "connection_error"}

    if not creds_json:
        logger.warning("[SHEETS] credentials missing for credential=%s", credential.id)
        return {"status": "error"}

    try:
        live_context, gs_hints = await fetch_google_sheet_runtime_context(sheet_id, creds_json, question=msg.text)
    except Exception as e:
        logger.error("Google Sheets fetch failed for credential=%s: %s", credential.id, e)
        return {"status": "connection_error"}

    sheet_filters = _extract_sheet_value_filters(msg.text, gs_hints)
    metadata_blueprint = credential.schema_blueprint or "No metadata analysis is stored for this Google Sheet yet."

    system_prompt = f"""You are {tenant.company_name}'s Google Sheets data analyst.
Answer using ONLY the GOOGLE SHEETS METADATA and LIVE DATA below.
Plain text only. ABSOLUTELY NO MARKDOWN. Do NOT use asterisks (*) for bold or italics. Do not use **text**. Keep it short: 3-8 lines.

GOOGLE SHEETS METADATA (metadata_analysis.json):
{metadata_blueprint}

LIVE GOOGLE SHEETS DATA:
{live_context}

{conversation_context_block}
RULES AND SEMANTIC HINTS:
{gs_hints}
{sheet_filters}
ANSWERING RULES:
- Treat each worksheet/tab as a table.
- If TARGETED ROW MATCHES are present, use those per-sheet counts/rows first.
  They are computed from all worksheet rows before the displayed snapshot is truncated.
- Use the FULL DATA SNAPSHOT rows as the source of truth for answers.
- Sample rows are only examples of structure; do not answer from samples when full rows are available.
- If the question names a sheet/tab, use that sheet first. Otherwise choose the sheet whose description and headers best match the question.
- For lookup questions like "record/person/customer/order named X", filter by the primary name/title/ID column for the selected sheet. Do not apply unrelated columns such as Manager/Owner/Department unless the user explicitly asks for that relationship or filter.
- For counts, sums, averages, maximums, and minimums, calculate from the matching rows. Do not estimate.
- For lookup questions, return the exact value from the matching row and the most relevant fields around it.
- Format one matching record using the selected sheet's actual schema, not a hard-coded business template.
- Field order for a record:
  1. Primary identifier/name/title fields first, using primary_keys and column_descriptions from metadata when available.
  2. Fields directly requested by the user.
  3. important_columns from metadata, in the order they appear there.
  4. Remaining useful fields in the same left-to-right order as the sheet headers.
- Group related fields only when the schema clearly supports it; otherwise use compact "Column: value" lines.
- For multiple records, number each record and keep the same schema-derived field order.
- For pending/incomplete/not done, apply the Status/Pending hints. Use blank completion/submission dates only when the hint says blank means pending.
- If a sheet says its data snapshot is truncated and the question needs all rows, say the exact answer needs a full snapshot instead of inventing a number.
- If the answer is not present in the context, say you could not find it in the sheet.

AVOID:
- Never force employee/HR-specific labels onto other schemas.
- Never mention filters that produced no match if another matching row exists through a better primary name/title/ID field.
- Never say "Based on the data provided".
- Never repeat every column name as a label on every row.
- Never add filler like "Let me know if you need more!"

LANGUAGE RULE:
Look at this exact user question: "{msg.text}"
Reply in the exact same language as that question. Database values in other languages must NOT influence your reply language.

USER QUESTION: {msg.text}""".strip()

    try:
        reply = await _call_openai_formatting(system_prompt, msg.text, max_tokens=600)
    except Exception as fmt_error:
        logger.error(f"[FORMAT_ERR] sheets credential={credential.id} {fmt_error}")
        return {"status": "error"}

    if not reply:
        return {"status": "error"}

    _remember_conversation_context(msg, msg.text, reply)
    return {"status": "ok", "reply": reply, "sql": None}

async def _handle_unboarded_client(msg: BotMessage, registered_client: Any) -> None:
    """Send a personalised onboarding link to a registered-but-not-yet-set-up client.

    If some products are already connected, sends an "add remaining DB" link instead.
    All failures are caught so they never surface as an unhandled exception to the caller.
    """
    from app.auth.onboarding_jwt import InvalidOnboardingTokenError, build_form_url, issue_token

    try:
        purchased: list[dict[str, Any]] = list(registered_client.purchased_products or [])

        # Determine which products already have a credential row in NeonDB.
        connected_slugs: set[str] = set()
        if registered_client.tenant_id is not None:
            all_creds = await get_tenant_credentials(registered_client.tenant_id)
            if all_creds is not None:
                # get_tenant_credentials returns a single row (legacy); fetch all rows instead.
                from app.database import session_factory
                from app.models import TenantDBCredential as _Cred
                from sqlalchemy import select as _select
                async with session_factory() as _s:
                    _res = await _s.execute(
                        _select(_Cred).where(_Cred.tenant_id == registered_client.tenant_id)
                    )
                    for cred_row in _res.scalars().all():
                        if cred_row.product_slug:
                            connected_slugs.add(cred_row.product_slug)

        unconnected = [p for p in purchased if p.get("slug") not in connected_slugs]

        if not unconnected:
            # All purchased products already have a credential — shouldn't normally reach
            # here, but handle gracefully.
            await send_reply(
                msg,
                f"Hi {registered_client.contact_name}! Your account is already set up. "
                "Try asking me a business question.",
            )
            return

        if not connected_slugs:
            # Nothing connected yet — initial setup link.
            purpose = "initial_setup"
            product_slug_for_token = None
            first_product = unconnected[0].get("display_name") or unconnected[0].get("slug", "")
        else:
            # Partially onboarded — link for the first remaining product.
            purpose = "add_database"
            product_slug_for_token = unconnected[0].get("slug")
            first_product = unconnected[0].get("display_name") or product_slug_for_token or ""

        try:
            token, _ = await issue_token(
                registered_client_id=registered_client.id,
                purpose=purpose,
                product_slug=product_slug_for_token,
            )
            form_url = build_form_url(token)
        except RuntimeError as cfg_err:
            logger.error("[ONBOARDING] Configuration error for client %s: %s", registered_client.id, cfg_err)
            await send_reply(
                msg,
                "Your account is registered but onboarding isn't configured yet. "
                "Please contact the Botivate team.",
            )
            return

        if not connected_slugs:
            reply = (
                f"Welcome to Botivate, {registered_client.contact_name} "
                f"from {registered_client.company_name}! "
                f"To connect your database, please use this link "
                f"(expires in 30 minutes):\n{form_url}"
            )
        else:
            reply = (
                f"You still need to connect your {first_product} database. "
                f"Use this link:\n{form_url}"
            )

        logger.info(
            "[ONBOARDING] Issued %s token for registered_client=%s purpose=%s product=%s",
            purpose,
            registered_client.id,
            purpose,
            product_slug_for_token,
        )
        await send_reply(msg, reply)

    except Exception:
        logger.exception(
            "[ONBOARDING] Unexpected error handling unboarded client chat_id=%s", msg.chat_id
        )
        await send_reply(
            msg,
            "Your account is registered but setup isn't complete yet. "
            "Please contact the Botivate team.",
        )

async def _handle_adddb_command(msg: BotMessage) -> None:
    """Handle the /adddb command — let an existing or not-yet-onboarded client connect a database."""
    from app.auth.onboarding_jwt import build_form_url, issue_token

    # registered_client is the source of truth for purchased_products.
    # A tenant row is NOT required — unboarded clients (tenant_id=None) can also use /adddb.
    registered_client = await find_registered_client_by_chat(
        msg.platform.value, msg.chat_id
    )
    if registered_client is None:
        await send_reply(
            msg,
            "I couldn't find your account. Please contact the Botivate team to get registered.",
        )
        return

    purchased: list[dict[str, Any]] = list(registered_client.purchased_products or [])
    if not purchased:
        await send_reply(
            msg,
            "No purchased products are associated with your account. "
            "Please contact the Botivate team.",
        )
        return

    # Determine which slugs already have a credential row (only possible if tenant exists)
    connected_slugs: set[str] = set()
    tenant = await get_tenant_by_chat_id(msg.chat_id)
    if tenant is not None:
        all_creds = await get_tenant_credentials_all(tenant.id)
        connected_slugs = {
            getattr(c, "product_slug", None)
            for c in all_creds
            if getattr(c, "product_slug", None)
        }

    unconnected = [p for p in purchased if p.get("slug") not in connected_slugs]

    if not unconnected:
        await send_reply(
            msg,
            "All your purchased products already have databases connected. "
            "Contact the Botivate team to add more products.",
        )
        return

    if len(unconnected) == 1:
        slug = unconnected[0].get("slug")
        display = unconnected[0].get("display_name") or slug or "database"
        purpose = "initial_setup" if tenant is None else "add_database"
        try:
            token, _ = await issue_token(
                registered_client_id=registered_client.id,
                purpose=purpose,
                product_slug=slug,
            )
            form_url = build_form_url(token)
        except RuntimeError as cfg_err:
            logger.error("[ADDDB] Configuration error for client %s: %s", registered_client.id, cfg_err)
            await send_reply(msg, "Onboarding is not configured yet. Please contact the Botivate team.")
            return
        await send_reply(
            msg,
            f"To connect your {display} database, use this link (expires in 30 minutes):\n{form_url}",
        )
        return

    # 2+ unconnected products — let the user pick
    if msg.platform == Platform.TELEGRAM:
        from app.platforms.telegram import send_message_with_keyboard

        buttons = [
            [{"text": p.get("display_name") or p.get("slug", ""), "callback_data": f"adddb_product:{p.get('slug', '')}"}]
            for p in unconnected
        ]
        try:
            await send_message_with_keyboard(
                msg.chat_id,
                "Which database would you like to connect?",
                inline_keyboard=buttons,
            )
        except Exception as exc:
            logger.error("[ADDDB] Failed to send keyboard: %s", exc)
            await send_reply(msg, "Something went wrong. Please try again.")
    else:
        # WhatsApp: numbered text list
        lines = ["Which database would you like to connect? Reply with a number:"]
        for i, p in enumerate(unconnected, start=1):
            lines.append(f"{i}. {p.get('display_name') or p.get('slug', '')}")
        await send_reply(msg, "\n".join(lines))

async def handle_adddb_callback(
    chat_id: str, callback_query_id: str, callback_data: str
) -> None:
    """Handle Telegram inline keyboard callback for /adddb product selection."""
    from app.auth.onboarding_jwt import build_form_url, issue_token
    from app.platforms.telegram import answer_callback_query

    await answer_callback_query(callback_query_id)

    slug = callback_data.removeprefix("adddb_product:").strip()
    if not slug:
        return

    registered_client = await find_registered_client_by_chat(Platform.TELEGRAM.value, chat_id)
    if registered_client is None:
        return

    purchased: list[dict[str, Any]] = list(registered_client.purchased_products or [])
    product = next((p for p in purchased if p.get("slug") == slug), None)
    display = (product.get("display_name") if product else None) or slug

    try:
        token, _ = await issue_token(
            registered_client_id=registered_client.id,
            purpose="add_database",
            product_slug=slug,
        )
        form_url = build_form_url(token)
    except Exception as exc:
        logger.error("[ADDDB_CB] Token issuance failed for chat_id=%s slug=%s: %s", chat_id, slug, exc)
        from app.platforms.telegram import send_message
        await send_message(chat_id, "Something went wrong. Please try again.")
        return

    from app.platforms.telegram import send_message
    await send_message(
        chat_id,
        f"To connect your {display} database, use this link (expires in 30 minutes):\n{form_url}",
    )

async def handle_message(msg: BotMessage) -> None:
    # Show typing indicator immediately so the user knows we're working
    try:
        if msg.platform == Platform.TELEGRAM:
            from app.platforms.telegram import send_typing
            await send_typing(msg.chat_id)
    except Exception:
        pass  # never block message processing for a typing indicator

    try:
        text_upper = msg.text.strip().upper()
        text_normalized = msg.text.strip().lower()

        token = None
        if msg.platform == Platform.TELEGRAM and text_upper.startswith("/START ") and len(msg.text.split()) > 1:
            token = msg.text.strip().split(" ", 1)[1]
        elif msg.platform == Platform.WHATSAPP and text_upper.startswith("START-"):
            token = msg.text.strip().split("-", 1)[1]

        if token:
            try:
                import jwt
                from app.database import update_tenant_chat_id

                secret = os.getenv("ADMIN_SECRET_TOKEN", "")
                payload = jwt.decode(token, secret, algorithms=["HS256"])
                tenant_id = payload.get("tenant_id")
                if tenant_id:
                    await update_tenant_chat_id(tenant_id, msg.platform, msg.chat_id)
                    await send_reply(msg, "Welcome to Botivate! Your account is officially linked. How can I assist you today?")
                    return
            except Exception as e:
                logger.error("Failed to process magic link token: %s", e)
                await send_reply(msg, "Sorry, your onboarding link is invalid or expired. Please request a new link.")
                return

        if (msg.platform == Platform.TELEGRAM and text_normalized == "/start") or (
            msg.platform == Platform.WHATSAPP and text_normalized == "start"
        ):
            # Build a contextual welcome with available data
            welcome = await _build_welcome_message(msg.chat_id)
            await send_reply(msg, welcome)
            return

        if text_normalized in ("help", "/help"):
            await send_reply(
                msg,
                "Here are some things you can ask me:\n\n"
                "• How many pending tasks are there?\n"
                "• Show tasks assigned to [name]\n"
                "• What is the email of [name]?\n"
                "• Count of tasks by department\n"
                "• List all employees\n\n"
                "Just type your question naturally!",
            )
            return

        if (msg.platform == Platform.TELEGRAM and text_normalized == "/adddb") or (
            msg.platform == Platform.WHATSAPP and text_normalized == "adddb"
        ):
            await _handle_adddb_command(msg)
            return

        # ── Run intent detection and tenant lookup concurrently ────────────
        intent_result, tenant = await asyncio.gather(
            detect_intent(msg.text),
            get_tenant_by_chat_id(msg.chat_id),
        )

        if intent_result == "off_topic":
            await send_reply(msg, pick_off_topic_reply(msg.text))
            return

        # ── Tier 1: fully onboarded tenant ───────────────────────────────────
        if tenant is None:
            # ── Tier 2: registered but not yet onboarded ──────────────────
            registered_client = await find_registered_client_by_chat(
                msg.platform.value, msg.chat_id
            )

            if registered_client is not None:
                await _handle_unboarded_client(msg, registered_client)
                return

            # ── Tier 3: not registered at all ─────────────────────────────
            await send_reply(
                msg,
                "Hi! I couldn't find your account. "
                "Please contact the Botivate team to get registered.",
            )
            return

        # ── Pick which DB(s) to query ───────────────────────────────────────
        connections = await route_question_to_database(tenant.id, msg.text)
        if not connections:
            await send_reply(msg, "I couldn't determine which database to query. Please rephrase.")
            return

        single_db = len(connections) == 1
        sections: list[tuple[str, str]] = []  # (display_name, reply_text)
        outcomes: list[str] = []

        for credential in connections:
            db_type = (credential.db_type or "").lower()
            if db_type == "postgresql":
                outcome = await _run_postgres_pipeline_for_credential(msg, tenant, credential)
            elif db_type == "google_sheets":
                outcome = await _run_sheets_pipeline_for_credential(msg, tenant, credential)
            else:
                logger.warning(
                    "[SQL_PIPELINE] Unsupported db_type=%r for credential %s; skipping.",
                    db_type, credential.id,
                )
                continue

            status = outcome.get("status", "error")
            outcomes.append(status)
            if status == "ok" and outcome.get("reply"):
                sections.append(
                    (
                        getattr(credential, "display_name", None)
                        or getattr(credential, "product_slug", None)
                        or "Database",
                        outcome["reply"],
                    )
                )

        if not sections:
            # Pick the most-informative reply for the user. Connection errors win over
            # soft errors win over empty results.
            if "connection_error" in outcomes:
                await send_reply(msg, DATABASE_CONNECTION_MESSAGE)
            elif "error" in outcomes:
                await send_reply(msg, RETRIEVAL_FAILURE_MESSAGE)
            else:
                await send_reply(msg, "I couldn't find any data matching your request.")
            return

        if single_db:
            # Preserve exact pre-routing UX: no attribution prefix.
            await send_reply(msg, sections[0][1])
        else:
            combined = "\n\n".join(f"From {name}:\n{body}" for name, body in sections)
            await send_reply(msg, combined)
    except Exception:
        logger.exception("Failed to process customer message for chat_id %s", msg.chat_id)

