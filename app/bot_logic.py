from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any

import httpx
from dotenv import load_dotenv

from .database import QueryExecutionError, execute_tenant_query, get_tenant_by_chat_id, get_tenant_credentials
from .platforms.base import BotMessage, Platform, send_reply

load_dotenv()

logger = logging.getLogger(__name__)

MISTRAL_API_KEY = os.getenv("MISTRAL_API_KEY", "")
MISTRAL_CHAT_COMPLETIONS_URL = "https://api.mistral.ai/v1/chat/completions"
SQL_GENERATION_MODEL = os.getenv("SQL_GENERATION_MODEL", "codestral-latest")
RESPONSE_FORMAT_MODEL = os.getenv("RESPONSE_FORMAT_MODEL", "mistral-small-latest")
ACCOUNT_NOT_FOUND_MESSAGE = "Hi! I couldn't find your account. Please contact support."
GENERIC_FAILURE_MESSAGE = "Sorry, I ran into an issue while processing your request. Please try again."
SQL_DEFAULT_ROW_LIMIT = int(os.getenv("SQL_DEFAULT_ROW_LIMIT", "50"))
SQL_FULL_ROW_LIMIT = int(os.getenv("SQL_FULL_ROW_LIMIT", "500"))


class NeedsClarificationError(Exception):
    """Raised when user input is ambiguous and needs follow-up."""


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


def _strip_code_fences(text: str) -> str:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json|sql)?", "", cleaned, flags=re.IGNORECASE).strip()
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3].strip()
    return cleaned


def _extract_json_dict(raw: str) -> dict[str, Any] | None:
    cleaned = _strip_code_fences(raw)
    try:
        parsed = json.loads(cleaned)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
    if not match:
        return None
    try:
        parsed = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _is_full_result_request(question: str) -> bool:
    return bool(re.search(r"\b(all|whole|full|complete|entire)\b", question, flags=re.IGNORECASE))


async def generate_sql_query(company_name: str, schema_blueprint: str, question: str) -> str:
    row_limit = SQL_FULL_ROW_LIMIT if _is_full_result_request(question) else SQL_DEFAULT_ROW_LIMIT
    today_utc = datetime.now(timezone.utc).date().isoformat()
    system_prompt = (
        f"You are the Text-to-SQL backend for {company_name}.\n"
        "You must translate the user's question into a valid PostgreSQL read-only query.\n"
        f"Today's UTC date is {today_utc}.\n\n"
        f"Database Schema:\n{schema_blueprint}\n\n"
        "Rules:\n"
        "1. Query must be SELECT/WITH only. Never generate INSERT/UPDATE/DELETE/DDL.\n"
        "2. Use exactly the table and column names provided in the blueprint.\n"
        "3. If a table name includes schema prefix (e.g., 'schema.table'), use the full name.\n"
        f"4. Add deterministic ORDER BY when returning lists.\n"
        f"5. By default use LIMIT {row_limit} unless the SQL already has a stricter limit.\n"
        f"6. If user asks for full/all/whole data, do not leave it unbounded: cap with LIMIT {SQL_FULL_ROW_LIMIT}.\n"
        "7. If the request is ambiguous or missing key filters, ask a clarification question.\n\n"
        "Return STRICT JSON only with keys:\n"
        "{\"sql\": string|null, \"clarification_question\": string|null}"
    )
    
    raw_output = await _call_mistral([
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": question}
    ], max_tokens=500, model=SQL_GENERATION_MODEL)

    payload = _extract_json_dict(raw_output)
    if payload is not None:
        clarification_question = payload.get("clarification_question")
        if isinstance(clarification_question, str) and clarification_question.strip():
            raise NeedsClarificationError(clarification_question.strip())
        sql_value = payload.get("sql")
        cleaned = sql_value.strip() if isinstance(sql_value, str) else ""
    else:
        cleaned = _strip_code_fences(raw_output)

    return cleaned


async def repair_sql_query(
    company_name: str,
    schema_blueprint: str,
    question: str,
    failed_sql: str,
    db_error: str,
) -> str:
    prompt = (
        f"You are fixing a failed SQL query for {company_name}.\n"
        f"Database schema:\n{schema_blueprint}\n\n"
        f"User question:\n{question}\n\n"
        f"Failed SQL:\n{failed_sql}\n\n"
        f"Database error:\n{db_error}\n\n"
        "Return STRICT JSON only with keys:\n"
        "{\"sql\": string, \"clarification_question\": string|null}\n"
        "Rules:\n"
        "- SELECT/WITH only.\n"
        "- Keep semantics of user question.\n"
        "- Fix table/column names and joins based on schema.\n"
        f"- Include LIMIT <= {SQL_FULL_ROW_LIMIT}."
    )
    raw = await _call_mistral(
        [{"role": "user", "content": prompt}],
        max_tokens=500,
        model=SQL_GENERATION_MODEL,
    )
    payload = _extract_json_dict(raw)
    if payload is not None:
        clarification_question = payload.get("clarification_question")
        if isinstance(clarification_question, str) and clarification_question.strip():
            raise NeedsClarificationError(clarification_question.strip())
        sql_value = payload.get("sql")
        return sql_value.strip() if isinstance(sql_value, str) else ""
    return _strip_code_fences(raw)


async def format_sql_response(company_name: str, question: str, sql_results: list[dict[str, Any]]) -> str:
    rows_json = json.dumps(sql_results, ensure_ascii=True, default=str)
    
    system_prompt = (
        f"You are the customer facing agent for {company_name}.\n"
        f"The user asked: '{question}'.\n"
        f"The database returned: {rows_json}\n\n"
        "Your task:\n"
        "- Use ONLY these rows as source of truth.\n"
        "- Do NOT invent dates, names, counts, or fields not present in rows.\n"
        "- If rows are list-like, present key rows in concise bullets.\n"
        "- If data is insufficient, say exactly what is missing.\n"
        "- Be friendly, natural, and concise.\n"
        "- Reply in exactly the same language the user wrote in."
    )
    
    reply = await _call_mistral([
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": "Please answer my question using the data provided."}
    ], max_tokens=600, model=RESPONSE_FORMAT_MODEL)
    return reply


def _validate_generated_sql(sql: str) -> str:
    cleaned = sql.strip()
    if not cleaned:
        raise ValueError("Generated SQL is empty.")

    # Permit a trailing semicolon only; block multi-statement queries.
    if cleaned.endswith(";"):
        cleaned = cleaned[:-1].strip()
    if ";" in cleaned:
        raise ValueError("Generated SQL contains multiple statements.")

    lowered = cleaned.lower()
    if not (lowered.startswith("select ") or lowered.startswith("with ")):
        raise ValueError("Generated SQL is not read-only.")

    blocked_patterns = (
        r"\binsert\b",
        r"\bupdate\b",
        r"\bdelete\b",
        r"\bdrop\b",
        r"\balter\b",
        r"\btruncate\b",
        r"\bcreate\b",
        r"\bgrant\b",
        r"\brevoke\b",
        r"\bcopy\b",
        r"\bexecute\b",
        r"\bcall\b",
        r"\bdo\b",
    )
    if any(re.search(pattern, lowered) for pattern in blocked_patterns):
        raise ValueError("Generated SQL includes disallowed operations.")

    return cleaned


def _format_rows_fallback(rows: list[dict[str, Any]], max_rows: int = 20) -> str:
    if not rows:
        return "I couldn't find any data matching your request."

    preview = rows[:max_rows]
    lines = [f"I found {len(rows)} record(s)."]
    for index, row in enumerate(preview, start=1):
        parts = [f"{key}: {value}" for key, value in row.items()]
        lines.append(f"{index}. " + " | ".join(parts))
    if len(rows) > max_rows:
        lines.append(f"...and {len(rows) - max_rows} more record(s).")
    return "\n".join(lines)


async def handle_message(msg: BotMessage) -> None:
    try:
        # Magic Link Onboarding Hooks
        text_upper = msg.text.strip().upper()
        text_normalized = msg.text.strip().lower()
        token = None
        if msg.platform == "telegram" and text_upper.startswith("/START ") and len(msg.text.split()) > 1:
            token = msg.text.strip().split(" ", 1)[1]
        elif msg.platform == "whatsapp" and text_upper.startswith("START-"):
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
                logger.error(f"Failed to process magic link token: {e}")
                await send_reply(msg, "Sorry, your onboarding link is invalid or expired. Please request a new link.")
                return

        if (msg.platform == Platform.TELEGRAM and text_normalized == "/start") or (
            msg.platform == Platform.WHATSAPP and text_normalized == "start"
        ):
            await send_reply(msg, "Hi! I'm ready. Ask me a business question and I'll fetch it from your data.")
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
            # 1. Ask LLM to generate SQL based on Blueprint
            blueprint = credentials.schema_blueprint or "No schema available."
            try:
                sql_query = await generate_sql_query(tenant.company_name, blueprint, msg.text)
                if not sql_query:
                    await send_reply(msg, "Could you clarify your request with more detail?")
                    return
                sql_query = _validate_generated_sql(sql_query)
            except NeedsClarificationError as e:
                await send_reply(msg, str(e))
                return
            except Exception as e:
                logger.error(f"LLM SQL generation failed: {e}")
                await send_reply(msg, GENERIC_FAILURE_MESSAGE)
                return
            
            logger.info(f"Generated SQL for tenant {tenant.id}: {sql_query}")
            
            # 2. Run SQL query
            try:
                query_rows = await execute_tenant_query(tenant.id, sql_query)
            except QueryExecutionError as e:
                logger.error("Tenant query failed (first attempt): %s", e)
                try:
                    repaired_sql = await repair_sql_query(
                        tenant.company_name,
                        blueprint,
                        msg.text,
                        failed_sql=sql_query,
                        db_error=str(e),
                    )
                    if not repaired_sql:
                        await send_reply(msg, GENERIC_FAILURE_MESSAGE)
                        return
                    repaired_sql = _validate_generated_sql(repaired_sql)
                    logger.info("Repaired SQL for tenant %s: %s", tenant.id, repaired_sql)
                    query_rows = await execute_tenant_query(tenant.id, repaired_sql)
                except NeedsClarificationError as repair_clarification:
                    await send_reply(msg, str(repair_clarification))
                    return
                except Exception as repair_error:
                    logger.error("Tenant query failed after SQL repair: %s", repair_error)
                    await send_reply(msg, GENERIC_FAILURE_MESSAGE)
                    return
                
            # 3. Ask LLM to format the output
            if not query_rows:
                await send_reply(msg, "I couldn't find any data matching your request.")
                return
            
            try:
                reply = await format_sql_response(tenant.company_name, msg.text, query_rows)
                if not reply.strip():
                    raise ValueError("Empty formatted reply.")
            except Exception as e:
                logger.error(f"LLM response formatting failed: {e}")
                await send_reply(msg, _format_rows_fallback(query_rows))
                return
            await send_reply(msg, reply)
            
        elif credentials.db_type.lower() == "google_sheets":
            # Decrypt sheet ID and service account credentials
            from .database import _decrypt_credential_value, fetch_google_sheet_data
            from cryptography.fernet import InvalidToken
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
                logger.error(f"Google Sheets fetch failed: {e}")
                await send_reply(msg, "I couldn't access your Google Sheet right now. Please try again.")
                return

            # Use LLM to answer directly from the snapshot data
            system_prompt = (
                f"You are the customer assistant for {tenant.company_name}.\n"
                f"Database Schema: {blueprint}\n\n"
                f"Live Data (first 50 rows per sheet):\n{data_snapshot}\n\n"
                "Instructions:\n"
                "- Answer the user's question using ONLY the data above.\n"
                "- Be concise and friendly.\n"
                "- Reply in exactly the same language the user wrote in."
            )
            reply = await _call_mistral([
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": msg.text}
            ], max_tokens=500, model=RESPONSE_FORMAT_MODEL)
            await send_reply(msg, reply or "I couldn't generate a response.")

            
    except Exception as e:
        logger.exception("Failed to process customer message for chat_id %s", msg.chat_id)
        try:
            await send_reply(msg, GENERIC_FAILURE_MESSAGE)
        except Exception:
            pass

__all__ = ["handle_message"]
