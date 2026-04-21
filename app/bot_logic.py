from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

import httpx
from dotenv import load_dotenv

from .database import QueryExecutionError, execute_tenant_query, get_active_modules, get_sql_template, get_tenant_by_chat_id
from .platforms.base import BotMessage, send_reply

load_dotenv()

logger = logging.getLogger(__name__)

MISTRAL_API_KEY = os.getenv("MISTRAL_API_KEY", "")
MISTRAL_CHAT_COMPLETIONS_URL = "https://api.mistral.ai/v1/chat/completions"
MISTRAL_MODEL = "mistral-small-latest"
ACCOUNT_NOT_FOUND_MESSAGE = "Hi! I couldn't find your account. Please contact Botivate support."
NO_RESULTS_MESSAGE = "I could not find matching records for your request. Please share more details."
SQL_TEMPLATE_NOT_FOUND_MESSAGE = "I can't answer that yet."
TENANT_QUERY_ERROR_MESSAGE = "I couldn't access your data right now. Please try again shortly."

MODULE_CAPABILITIES: dict[str, str] = {
	"minutes_of_meeting": "Manage and explain meeting schedules, MoM summaries, action items, and attendee follow-ups.",
	"delivery_tracker": "Track delivery progress, estimated dates, delays, and delivery confirmations.",
	"general": "Answer general product and platform questions that are not tied to one customer purchase record.",
}

GENERAL_PRODUCT_KNOWLEDGE = (
	"Botivate is a WhatsApp-first customer engagement assistant. "
	"It helps customers with product guidance, meeting support workflows, and delivery tracking updates. "
	"Available modules include minutes_of_meeting and delivery_tracker."
)


def _format_module_capabilities(active_modules: list[str]) -> str:
	lines: list[str] = []
	for module in active_modules:
		capability = MODULE_CAPABILITIES.get(module, "General customer support capability.")
		lines.append(f"- {module}: {capability}")
	return "\n".join(lines)


def _build_general_system_prompt(active_modules: list[str], intent_payload: dict[str, Any], company_name: str) -> str:
	module_capabilities = _format_module_capabilities(active_modules)
	intent = intent_payload.get("intent", "general_query")
	entities = intent_payload.get("entities", {})

	return (
		f"You are Botivate support assistant for tenant company {company_name}.\n\n"
		f"Product Knowledge:\n{GENERAL_PRODUCT_KNOWLEDGE}\n\n"
		f"Available Modules and Capabilities:\n{module_capabilities}\n\n"
		f"Detected Intent: {intent}\n"
		f"Detected Entities: {json.dumps(entities, ensure_ascii=True, default=str)}\n\n"
		"Instructions:\n"
		"- Answer using product knowledge only; do not invent customer-specific purchase details.\n"
		"- Be concise and friendly.\n"
		"- Reply in the same language the customer writes in.\n"
		"- If asked to book a technician or escalate, say the team will call within 24 hours.\n"
	)


def _build_module_system_prompt(
	company_name: str,
	module: str,
	intent: str,
	entities: dict[str, Any],
	query_rows: list[dict[str, Any]],
) -> str:
	rows_json = json.dumps(query_rows, ensure_ascii=True, default=str)

	return (
		f"You are Botivate support assistant for tenant company {company_name}.\n\n"
		f"Module: {module}\n"
		f"Intent: {intent}\n"
		f"Entities: {json.dumps(entities, ensure_ascii=True, default=str)}\n"
		f"Query Result Rows (authoritative): {rows_json}\n\n"
		"Instructions:\n"
		"- Answer only using the query result rows provided above.\n"
		"- If data is missing, say it clearly and ask a concise follow-up question.\n"
		"- Be concise and friendly.\n"
		"- Reply in the same language the customer writes in.\n"
		"- If asked to book a technician or escalate, say the team will call within 24 hours.\n"
	)


def _extract_entity_string(value: Any) -> str | None:
	if isinstance(value, str):
		cleaned = value.strip()
		return cleaned or None

	if isinstance(value, list):
		for item in value:
			if isinstance(item, str):
				cleaned = item.strip()
				if cleaned:
					return cleaned

	return None


def _normalize_query_param(key: str, value: Any) -> Any:
	if value is None:
		return None

	if isinstance(value, (int, float, bool)):
		return value

	string_value = _extract_entity_string(value)
	if string_value is None:
		return None

	lower_key = key.lower()
	if any(token in lower_key for token in ["name", "status"]):
		return string_value if "%" in string_value else f"%{string_value}%"

	return string_value


def _build_query_params(entities: dict[str, Any]) -> tuple[Any, ...]:
	if not isinstance(entities, dict):
		return ()

	params: list[Any] = []
	ordered_keys = [
		"employee_name",
		"employee",
		"customer_name",
		"customer",
		"name",
		"names",
		"status",
		"statuses",
		"date",
		"dates",
	]
	remaining_keys = [key for key in entities.keys() if key not in ordered_keys]

	for key in [*ordered_keys, *sorted(remaining_keys)]:
		normalized = _normalize_query_param(key, entities.get(key))
		if normalized is not None:
			params.append(normalized)

	return tuple(params)


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

	if isinstance(content, list):
		text_chunks: list[str] = []
		for item in content:
			if isinstance(item, dict):
				text_value = item.get("text")
				if isinstance(text_value, str):
					text_chunks.append(text_value)
		return "".join(text_chunks).strip()

	return ""


def _parse_classifier_json(raw_text: str) -> dict[str, Any]:
	cleaned = raw_text.strip()

	if cleaned.startswith("```"):
		cleaned = re.sub(r"^```(?:json)?", "", cleaned, flags=re.IGNORECASE).strip()
		if cleaned.endswith("```"):
			cleaned = cleaned[:-3].strip()

	try:
		parsed = json.loads(cleaned)
		if isinstance(parsed, dict):
			return parsed
	except json.JSONDecodeError:
		pass

	match = re.search(r"\{[\s\S]*\}", cleaned)
	if match:
		try:
			parsed = json.loads(match.group(0))
			if isinstance(parsed, dict):
				return parsed
		except json.JSONDecodeError:
			return {}

	return {}


async def _call_mistral(messages: list[dict[str, str]], max_tokens: int) -> str:
	if not MISTRAL_API_KEY:
		raise RuntimeError("MISTRAL_API_KEY is not configured in .env.")

	payload = {
		"model": MISTRAL_MODEL,
		"messages": messages,
		"max_tokens": max_tokens,
	}
	headers = {
		"Authorization": f"Bearer {MISTRAL_API_KEY}",
		"Content-Type": "application/json",
	}

	async with httpx.AsyncClient(timeout=45.0) as client:
		# SWITCH_TO_CLAUDE
		response = await client.post(MISTRAL_CHAT_COMPLETIONS_URL, headers=headers, json=payload)

	response.raise_for_status()
	response_data = response.json()
	assistant_text = _extract_assistant_text(response_data)
	return assistant_text or "Sorry, I could not generate a response right now."


async def _generate_reply_with_mistral(system_prompt: str, user_message: str) -> str:
	return await _call_mistral(
		messages=[
			{"role": "system", "content": system_prompt},
			{"role": "user", "content": user_message},
		],
		max_tokens=500,
	)


async def format_reply(
	company_name: str,
	user_message: str,
	module: str,
	intent: str,
	entities: dict[str, Any],
	query_rows: list[dict[str, Any]],
) -> str:
	system_prompt = _build_module_system_prompt(company_name, module, intent, entities, query_rows)
	return await _generate_reply_with_mistral(system_prompt, user_message)


async def classify_intent(question: str, active_modules: list[str]) -> dict[str, Any]:
	modules = active_modules if active_modules else ["general"]
	module_capabilities = _format_module_capabilities(modules)

	classification_system_prompt = (
		"You are an intent classifier for Botivate.\n\n"
		f"Available modules and capabilities:\n{module_capabilities}\n\n"
		"Valid intents for minutes_of_meeting: meeting_schedule, task_status.\n"
		"Valid intents for delivery_tracker: delivery_status.\n\n"
		"Classify the user question into the best module and ONLY use the exact valid intent strings listed above.\n"
		"Return ONLY valid JSON with this shape:\n"
		'{"module": "string", "intent": "string", "entities": {"names": [], "dates": [], "statuses": []}}\n'
		f"module must be one of: {', '.join(modules)}.\n"
		"Use module=general for product-level or generic questions not requiring customer purchase lookup."
	)

	try:
		classifier_output = await _call_mistral(
			messages=[
				{"role": "system", "content": classification_system_prompt},
				{"role": "user", "content": question},
			],
			max_tokens=220,
		)
		parsed = _parse_classifier_json(classifier_output)
	except Exception:
		logger.exception("Intent classification failed; falling back to default classification.")
		parsed = {}

	module = parsed.get("module") if isinstance(parsed.get("module"), str) else "general"
	if module not in modules:
		module = "general" if "general" in modules else modules[0]

	intent = parsed.get("intent") if isinstance(parsed.get("intent"), str) else "general_query"
	entities = parsed.get("entities") if isinstance(parsed.get("entities"), dict) else {}

	return {
		"module": module,
		"intent": intent.strip() or "general_query",
		"entities": entities,
	}


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
				import jwt, os
				from .database import update_tenant_chat_id
				secret = os.getenv("ADMIN_SECRET_TOKEN", "")
				payload = jwt.decode(token, secret, algorithms=["HS256"])
				tenant_id = payload.get("tenant_id")
				if tenant_id:
					await update_tenant_chat_id(tenant_id, msg.platform, msg.chat_id)
					await send_reply(msg, "Welcome to Botivate! Your account is officially linked successfully. How can I assist you today?")
					return
			except Exception as e:
				logger.error(f"Failed to process magic link token: {e}")
				await send_reply(msg, "Sorry, your onboarding link is invalid or expired. Please request a new link.")
				return

		tenant = await get_tenant_by_chat_id(msg.chat_id)
		if tenant is None:
			await send_reply(msg, ACCOUNT_NOT_FOUND_MESSAGE)
			return

		active_modules = await get_active_modules(tenant.id)
		classification_modules = list(dict.fromkeys([*active_modules, "general"])) if active_modules else ["general"]

		intent_payload = await classify_intent(msg.text, classification_modules)
		module = intent_payload.get("module", "general")
		intent = intent_payload.get("intent", "general_query")
		entities = intent_payload.get("entities", {}) if isinstance(intent_payload.get("entities"), dict) else {}

		if intent_payload.get("module") == "general":
			general_system_prompt = _build_general_system_prompt(
				classification_modules,
				intent_payload,
				tenant.company_name,
			)
			assistant_reply = await _generate_reply_with_mistral(general_system_prompt, msg.text)
			await send_reply(msg, assistant_reply)
			return

		sql_template = await get_sql_template(tenant.id, module, intent)
		if not sql_template:
			await send_reply(msg, SQL_TEMPLATE_NOT_FOUND_MESSAGE)
			return

		query_params = _build_query_params(entities)

		try:
			query_rows = await execute_tenant_query(tenant.id, sql_template, *query_params)
		except QueryExecutionError:
			logger.exception("Tenant query execution failed for tenant %s", tenant.id)
			await send_reply(msg, TENANT_QUERY_ERROR_MESSAGE)
			return

		if not query_rows:
			await send_reply(msg, NO_RESULTS_MESSAGE)
			return

		assistant_reply = await format_reply(
			tenant.company_name,
			msg.text,
			module,
			intent,
			entities,
			query_rows,
		)
		await send_reply(msg, assistant_reply)
	except Exception:
		logger.exception("Failed to process customer message for chat_id %s", msg.chat_id)
		try:
			await send_reply(msg, "Sorry, something went wrong. Please try again in a moment.")
		except Exception:
			logger.exception("Failed to send fallback reply for chat_id %s", msg.chat_id)


__all__ = ["classify_intent", "handle_message"]
