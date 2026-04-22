from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

import httpx
from dotenv import load_dotenv

from .database import QueryExecutionError, execute_tenant_query, get_tenant_by_chat_id, get_tenant_credentials
from .platforms.base import BotMessage, send_reply

load_dotenv()

logger = logging.getLogger(__name__)

MISTRAL_API_KEY = os.getenv("MISTRAL_API_KEY", "")
MISTRAL_CHAT_COMPLETIONS_URL = "https://api.mistral.ai/v1/chat/completions"

# Dual-model architecture: Codestral for SQL, Mistral Small for chat
MISTRAL_SQL_MODEL = os.getenv("MISTRAL_SQL_MODEL", "codestral-latest")
MISTRAL_CHAT_MODEL = os.getenv("MISTRAL_CHAT_MODEL", "mistral-small-latest")

ACCOUNT_NOT_FOUND_MESSAGE = "Hi! I couldn't find your account. Please contact support."


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


async def _call_mistral(messages: list[dict[str, str]], max_tokens: int, *, model: str | None = None) -> str:
    if not MISTRAL_API_KEY:
        raise RuntimeError("MISTRAL_API_KEY is not configured in .env.")

    payload = {
        "model": model or MISTRAL_CHAT_MODEL,
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


async def generate_sql_query(company_name: str, schema_blueprint: str, question: str) -> str:
    system_prompt = (
        f"You are the Text-to-SQL backend for {company_name}.\n"
        f"You must translate the user's question into a valid PostgreSQL SELECT query.\n\n"
        f"Database Schema:\n{schema_blueprint}\n\n"
        "Rules:\n"
        "1. Return ONLY the raw SQL query. No markdown, no explanations.\n"
        "2. Query must be a SELECT statement to answer the user's question.\n"
        "3. Use exactly the table and column names provided in the blueprint.\n"
        "4. If a table name includes a schema prefix (e.g., 'schema.table'), you MUST use the full name.\n"
        "5. Limit to the top 10 rows if applicable.\n"
        "6. For enum columns, use ONLY the exact allowed values shown in the schema (e.g., enum(val1, val2)).\n"
        "7. Use ILIKE for text matching to handle case differences."
    )
    
    raw_output = await _call_mistral([
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": question}
    ], max_tokens=300, model=MISTRAL_SQL_MODEL)
    
    # Strip markdown block quotes if the LLM adds them
    cleaned = raw_output.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:sql)?", "", cleaned, flags=re.IGNORECASE).strip()
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3].strip()
            
    return cleaned


async def format_sql_response(company_name: str, question: str, sql_results: list[dict[str, Any]]) -> str:
    rows_json = json.dumps(sql_results, ensure_ascii=True, default=str)
    
    system_prompt = (
        f"You are the customer facing agent for {company_name}.\n"
        f"The user asked: '{question}'.\n"
        f"The database returned: {rows_json}\n\n"
        "Your task:\n"
        "- Read the database rows and directly answer the user's question.\n"
        "- Be friendly, natural, and concise.\n"
        "- Reply in exactly the same language the user wrote in."
    )
    
    reply = await _call_mistral([
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": "Please answer my question using the data provided."}
    ], max_tokens=400)
    return reply


async def handle_message(msg: BotMessage) -> None:
    try:
        # Magic Link Onboarding Hooks
        text_upper = msg.text.strip().upper()
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
            except Exception as e:
                logger.error(f"LLM SQL generation failed: {e}")
                await send_reply(msg, f"LLM Error: Could not generate SQL query. Details: {e}")
                return
            
            logger.info(f"Generated SQL for tenant {tenant.id}: {sql_query}")
            
            # 2. Run SQL query
            try:
                query_rows = await execute_tenant_query(tenant.id, sql_query)
            except QueryExecutionError as e:
                await send_reply(msg, f"Database Error:\nQuery: {sql_query}\nDetails: {e}")
                return
                
            # 3. Ask LLM to format the output
            if not query_rows:
                await send_reply(msg, "I couldn't find any data matching your request.")
                return
            
            try:
                reply = await format_sql_response(tenant.company_name, msg.text, query_rows)
            except Exception as e:
                logger.error(f"LLM response formatting failed: {e}")
                await send_reply(msg, f"I found the data but couldn't format a response. Raw results: {query_rows[:3]}")
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
            ], max_tokens=500)
            await send_reply(msg, reply or "I couldn't generate a response.")

            
    except Exception as e:
        logger.exception("Failed to process customer message for chat_id %s", msg.chat_id)
        try:
            await send_reply(msg, f"Unhandled Error: {type(e).__name__}: {e}")
        except Exception:
            pass

__all__ = ["handle_message"]
