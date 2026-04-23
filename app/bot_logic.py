from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any

import httpx
from dotenv import load_dotenv
try:
    from openai import AsyncOpenAI
except ImportError as _openai_error:  # pragma: no cover - exercised in environments without openai installed
    AsyncOpenAI = Any  # type: ignore[assignment]
else:
    _openai_error = None

from .database import (
    QueryExecutionError,
    SecurityError,
    TenantDBConnectionError,
    execute_tenant_query,
    get_tenant_by_chat_id,
    get_tenant_credentials,
)
from .platforms.base import BotMessage, Platform, send_reply

load_dotenv()

logger = logging.getLogger(__name__)

MISTRAL_API_KEY = os.getenv("MISTRAL_API_KEY", "")
MISTRAL_CHAT_COMPLETIONS_URL = "https://api.mistral.ai/v1/chat/completions"
RESPONSE_FORMAT_MODEL = os.getenv("RESPONSE_FORMAT_MODEL", "mistral-large-2512")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
SQL_GENERATION_MODEL = os.getenv("SQL_GENERATION_MODEL", "gpt-4o")
ACCOUNT_NOT_FOUND_MESSAGE = "Hi! I couldn't find your account. Please contact support."
GENERIC_FAILURE_MESSAGE = "Sorry, I ran into an issue while processing your request. Please try again."
RETRIEVAL_FAILURE_MESSAGE = "I wasn't able to retrieve that information right now. Please try rephrasing your question."
DATABASE_CONNECTION_MESSAGE = (
    "I'm having trouble connecting to your database right now. "
    "Please contact Botivate support if this persists."
)

_openai_client: AsyncOpenAI | None = None


def _get_openai_client() -> AsyncOpenAI:
    global _openai_client
    if _openai_error is not None:
        raise RuntimeError("openai package is not installed. Add it to environment with pip install -r requirements.txt.")
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY is not configured in .env.")
    if _openai_client is None:
        _openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY)
    return _openai_client


def _extract_assistant_text(response_data: dict[str, Any]) -> str:
    choices = response_data.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""

    message = choices[0].get("message")
    if not isinstance(message, dict):
        return ""

    content = message.get("content")
    if isinstance(content, str):
        return content.strip()

    return ""


async def _call_mistral(messages: list[dict[str, str]], max_tokens: int, model: str) -> str:
    if not MISTRAL_API_KEY:
        raise RuntimeError("MISTRAL_API_KEY is not configured in .env.")

    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
    }
    headers = {
        "Authorization": f"Bearer {MISTRAL_API_KEY}",
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=45.0) as client:
        response = await client.post(MISTRAL_CHAT_COMPLETIONS_URL, headers=headers, json=payload)

    response.raise_for_status()
    response_data = response.json()
    assistant_text = _extract_assistant_text(response_data)
    return assistant_text or ""


async def _call_openai_sql(system_prompt: str, user_prompt: str) -> str:
    client = _get_openai_client()
    completion = await client.chat.completions.create(
        model=SQL_GENERATION_MODEL,
        temperature=0,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    )
    content = completion.choices[0].message.content
    return (content or "").strip()


def _strip_code_fences(text: str) -> str:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:sql|json)?", "", cleaned, flags=re.IGNORECASE).strip()
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3].strip()
    return cleaned


def _extract_entities(question: str) -> dict[str, Any]:
    dates = re.findall(r"\b\d{4}-\d{2}-\d{2}\b", question)
    emails = re.findall(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", question)
    numbers = re.findall(r"\b\d+(?:\.\d+)?\b", question)
    quoted_terms = re.findall(r"['\"]([^'\"]+)['\"]", question)

    return {
        "dates": dates,
        "emails": emails,
        "numbers": numbers,
        "quoted_terms": quoted_terms,
        "today_utc": datetime.now(timezone.utc).date().isoformat(),
    }


def _asks_for_everything(question: str) -> bool:
    return bool(re.search(r"\b(all|everything|entire|whole|complete)\b", question, flags=re.IGNORECASE))


async def is_off_topic(text: str) -> bool:
    prompt = (
        "Does this message relate to business data queries like tasks, "
        "meetings, deliveries, employees, or reports? "
        "Reply only YES or NO.\n"
        f"Message: {text}"
    )
    try:
        answer = await _call_mistral(
            [{"role": "user", "content": prompt}],
            max_tokens=8,
            model=RESPONSE_FORMAT_MODEL,
        )
    except Exception as exc:
        logger.warning("Off-topic detection failed open: %s", exc)
        return False

    return answer.strip().upper().startswith("NO")


def detect_multi_table_query(text: str) -> bool:
    patterns = (
        "each table",
        "all tables",
        "every table",
        "from all",
        "list tables",
        "show tables",
    )
    lowered = text.lower()
    return any(p in lowered for p in patterns)


def _extract_table_names_from_blueprint(schema_blueprint: str) -> list[str]:
    return re.findall(r"^Table `([^`]+)`", schema_blueprint, flags=re.MULTILINE)


def build_table_aliases(schema_blueprint: str) -> str:
    table_names = _extract_table_names_from_blueprint(schema_blueprint)
    if not table_names:
        return "No tables found."

    aliases = {}
    used = set()
    for table in table_names:
        for length in range(1, len(table) + 1):
            candidate = table[:length].lower()
            if candidate not in used:
                aliases[table] = candidate
                used.add(candidate)
                break

    return ", ".join(f"{t} -> {a}" for t, a in aliases.items())


async def generate_sql_query(
    company_name: str,
    schema_blueprint: str,
    question: str,
    tenant_query_hints: str | None = None,
) -> str:
    entities = _extract_entities(question)
    dynamic_aliases = build_table_aliases(schema_blueprint)

    hints_section = ""
    if tenant_query_hints and tenant_query_hints.strip():
        hints_section = f"""
--- CLIENT-SPECIFIC HINTS ---
{tenant_query_hints.strip()}
"""

    system_prompt = f"""
You are an expert PostgreSQL data analyst. Your only job is to
write a single, correct SQL SELECT query based on the user's
question and the database schema provided below.

--- DATABASE SCHEMA ---
{schema_blueprint}

--- TABLE ALIASES ---
Use these short aliases for all tables:
{dynamic_aliases}
{hints_section}
--- OUTPUT RULES ---
- Output ONLY the raw SQL query
- No markdown, no backticks, no explanation, no comments
- No semicolon at the end
- Only SELECT statements -- never INSERT, UPDATE, DELETE,
  DROP, ALTER, CREATE, TRUNCATE, GRANT, REVOKE

--- QUERY WRITING RULES ---
- Always use the table aliases defined above
- Never use SELECT * -- always list column names explicitly
- Use ILIKE for all text/name searches, never exact match
- Add LIMIT 50 unless user asks for all records
- For JOINs, use FK relationships shown in the schema above
- Prefer LEFT JOIN over INNER JOIN unless certain every
  row has a match
- If a column or table is not in the schema, do not invent it

--- DATE AND TIME RULES ---
- Use CURRENT_DATE for today's date
- For month queries use EXTRACT(MONTH FROM col) and
  EXTRACT(YEAR FROM col)
- Never assume a date column name -- always check the schema

--- COUNTING RULES ---
- Always apply all relevant WHERE filters before counting
- Never COUNT(*) a full table when a filtered count is implied
- If counting items in a specific time period, filter by that
  period explicitly

--- MULTI-TABLE RULES ---
- If user asks for data from multiple tables separately,
  use UNION ALL with a literal 'table_source' column to
  identify which table each row came from
- Match column count and compatible types across UNION ALL parts

--- VALIDATION SELF-CHECK ---
Before writing the final query, verify:
1. Does every column I use exist in the schema above?
2. Are JOINs using the correct FK columns?
3. Is WHERE filtering correctly for this question?
4. Does the expected result make logical sense?
5. Am I only selecting columns relevant to the question?

--- USER QUESTION ---
{question}

--- EXTRACTED ENTITIES ---
{json.dumps(entities, default=str)}

Write the SQL query now:
""".strip()

    user_prompt = f"Generate the SQL query for: {question}"
    raw_sql = await _call_openai_sql(system_prompt, user_prompt)
    return _strip_code_fences(raw_sql)


async def fix_sql(sql: str, error: str, schema_blueprint: str) -> str:
    system_prompt = (
        "You fix malformed PostgreSQL SELECT queries.\n"
        f"Schema blueprint:\n{schema_blueprint}\n\n"
        "Return ONLY corrected raw SQL query. No markdown, no explanation.\n"
        "Only SELECT statements are allowed."
    )
    user_prompt = f"Broken SQL:\n{sql}\n\nPostgreSQL error:\n{error}"

    fixed_sql = await _call_openai_sql(system_prompt, user_prompt)
    return _strip_code_fences(fixed_sql)


async def format_sql_response(company_name: str, question: str, sql_results: list[dict[str, Any]]) -> str:
    rows_json = json.dumps(sql_results, ensure_ascii=True, default=str)

    system_prompt = (
        f"You are the customer facing agent for {company_name}.\n"
        f"The user asked: '{question}'.\n"
        f"The database returned: {rows_json}\n\n"
        "Your task:\n"
        "- Read the database rows and directly answer the user's question.\n"
        "- Do not invent data not present in rows.\n"
        "- Be friendly, natural, and concise.\n"
        "- Reply in exactly the same language the user wrote in."
    )

    reply = await _call_mistral(
        [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": "Please answer my question using the data provided."},
        ],
        max_tokens=600,
        model=RESPONSE_FORMAT_MODEL,
    )
    return reply


def _validate_generated_sql(sql: str) -> str:
    cleaned = sql.strip().rstrip(";").strip()
    if not cleaned:
        raise ValueError("Generated SQL is empty.")

    lowered = cleaned.lower()
    if not lowered.startswith("select"):
        raise ValueError("Generated SQL is not a SELECT statement.")

    blocked_patterns = (
        r"\binsert\b",
        r"\bupdate\b",
        r"\bdelete\b",
        r"\bdrop\b",
        r"\btruncate\b",
        r"\balter\b",
        r"\bcreate\b",
        r"\bgrant\b",
        r"\brevoke\b",
    )
    if any(re.search(pattern, lowered) for pattern in blocked_patterns):
        raise ValueError("Generated SQL includes disallowed operations.")

    if re.search(r"\bselect\s+\*", lowered):
        raise ValueError("Generated SQL uses SELECT * which is not allowed.")

    return cleaned


async def handle_message(msg: BotMessage) -> None:
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
                from .database import update_tenant_chat_id

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
            await send_reply(msg, "Hi! I'm ready. Ask me a business question and I'll fetch it from your data.")
            return

        if await is_off_topic(msg.text):
            await send_reply(
                msg,
                "I'm Botivate Bot — I can only help you query your business data. "
                "Try asking about tasks, meetings, deliveries, or your team.",
            )
            return

        tenant = await get_tenant_by_chat_id(msg.chat_id)
        if tenant is None:
            await send_reply(msg, ACCOUNT_NOT_FOUND_MESSAGE)
            return

        credentials = await get_tenant_credentials(tenant.id)
        if not credentials:
            await send_reply(msg, "Your database connection is not fully configured.")
            return

        if credentials.db_type.lower() == "postgresql":
            blueprint = credentials.schema_blueprint or "No schema available."
            final_error: str | None = None
            sql_query = ""
            query_rows: list[dict[str, Any]] = []

            if detect_multi_table_query(msg.text):
                table_names = _extract_table_names_from_blueprint(blueprint)
                logger.info(f"[SQL_GEN] tenant={tenant.id} query='{msg.text}'")
                if not table_names:
                    await send_reply(msg, "I couldn't find any tables in the schema blueprint for this tenant.")
                    return

                combined_rows: list[dict[str, Any]] = []
                for table_name in table_names:
                    table_sql = f"SELECT * FROM {table_name} LIMIT 2"
                    logger.info(f"[SQL_OUT] {table_sql}")
                    try:
                        rows = await execute_tenant_query(tenant.id, table_sql, allow_select_star=True)
                        for row in rows:
                            normalized = dict(row)
                            normalized["table_source"] = table_name
                            combined_rows.append(normalized)
                        logger.info(f"[SQL_OK] rows_returned={len(rows)}")
                    except TenantDBConnectionError as e:
                        logger.error(f"[SQL_ERR] attempt=1 error='{e}'")
                        await send_reply(msg, DATABASE_CONNECTION_MESSAGE)
                        return
                    except (QueryExecutionError, SecurityError) as e:
                        logger.error(f"[SQL_ERR] attempt=1 error='{e}'")
                query_rows = combined_rows
            else:
                # ── SQL GENERATION (GPT-4o mini) ──
                sql_query = await generate_sql_query(
                    tenant.company_name,
                    blueprint,
                    msg.text,
                    tenant_query_hints=getattr(credentials, "tenant_query_hints", None),
                )
                sql_query = _validate_generated_sql(sql_query)
                logger.info(f"[SQL_GEN] tenant={tenant.id} query='{msg.text}'")
                logger.info(f"[SQL_OUT] {sql_query}")

                max_retries = 2
                attempt = 0
                while True:
                    try:
                        query_rows = await execute_tenant_query(tenant.id, sql_query)
                        logger.info(f"[SQL_OK] rows_returned={len(query_rows)}")
                        break
                    except TenantDBConnectionError as exec_error:
                        logger.error(f"[SQL_ERR] attempt={attempt + 1} error='{exec_error}'")
                        await send_reply(msg, DATABASE_CONNECTION_MESSAGE)
                        return
                    except (QueryExecutionError, SecurityError) as exec_error:
                        final_error = str(exec_error)
                        logger.error(f"[SQL_ERR] attempt={attempt + 1} error='{final_error}'")
                        if attempt >= max_retries:
                            logger.error(
                                f"[SQL_FAILED] tenant={tenant.id} question='{msg.text}' "
                                f"final_sql='{sql_query}' error='{final_error}'"
                            )
                            await send_reply(msg, RETRIEVAL_FAILURE_MESSAGE)
                            return
                        attempt += 1
                        sql_query = await fix_sql(sql_query, final_error, blueprint)
                        sql_query = _validate_generated_sql(sql_query)
                        logger.info(f"[SQL_OUT] {sql_query}")

            if not query_rows:
                await send_reply(msg, "I couldn't find any data matching your request.")
                return

            # ── REPLY FORMATTING (Mistral) ──
            reply = await format_sql_response(tenant.company_name, msg.text, query_rows)
            await send_reply(msg, reply or "I couldn't generate a response from the returned records.")
            return

        if credentials.db_type.lower() == "google_sheets":
            from cryptography.fernet import InvalidToken
            from .database import _decrypt_credential_value, fetch_google_sheet_data

            try:
                decrypted_url = _decrypt_credential_value(credentials.connection_url)
                sheet_id = decrypted_url.replace("google_sheets://", "")
                creds_json = _decrypt_credential_value(credentials.google_credentials) if credentials.google_credentials else None
            except (InvalidToken, Exception):
                await send_reply(msg, "Your Google Sheets credentials could not be decrypted. Please contact support.")
                return

            if not creds_json:
                await send_reply(msg, "Google Sheets credentials are not configured.")
                return

            try:
                blueprint, data_snapshot = fetch_google_sheet_data(sheet_id, creds_json)
            except Exception as e:
                logger.error("Google Sheets fetch failed: %s", e)
                await send_reply(msg, "I couldn't access your Google Sheet right now. Please try again.")
                return

            system_prompt = (
                f"You are the customer assistant for {tenant.company_name}.\n"
                f"Database Schema: {blueprint}\n\n"
                f"Live Data (first 50 rows per sheet):\n{data_snapshot}\n\n"
                "Instructions:\n"
                "- Answer the user's question using ONLY the data above.\n"
                "- Be concise and friendly.\n"
                "- Reply in exactly the same language the user wrote in."
            )
            reply = await _call_mistral(
                [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": msg.text},
                ],
                max_tokens=500,
                model=RESPONSE_FORMAT_MODEL,
            )
            await send_reply(msg, reply or "I couldn't generate a response.")
            return

        await send_reply(msg, "Unsupported tenant data source configuration.")
    except Exception:
        logger.exception("Failed to process customer message for chat_id %s", msg.chat_id)
        try:
            await send_reply(msg, RETRIEVAL_FAILURE_MESSAGE)
        except Exception:
            pass


__all__ = ["handle_message", "fix_sql", "generate_sql_query"]
