import asyncio
import logging
import os
from typing import Any

from dotenv import load_dotenv
from fastapi import APIRouter, Query, Request, Response

from .bot_logic import handle_message
from .platforms.base import BotMessage, Platform

load_dotenv()

logger = logging.getLogger(__name__)
WEBHOOK_VERIFY_TOKEN = os.getenv("WEBHOOK_VERIFY_TOKEN", "")

router = APIRouter(tags=["webhook"])


@router.post("/webhook/telegram")
async def telegram_webhook(request: Request) -> dict[str, bool]:
	try:
		data: dict[str, Any] = await request.json()

		message = data.get("message")
		if not isinstance(message, dict):
			return {"ok": True}

		chat = message.get("chat")
		if not isinstance(chat, dict):
			return {"ok": True}

		chat_id_value = chat.get("id")
		if chat_id_value is None:
			return {"ok": True}

		text = message.get("text", "")
		if not isinstance(text, str) or not text.strip():
			return {"ok": True}

		msg = BotMessage(platform=Platform.TELEGRAM, chat_id=str(chat_id_value), text=text)
		asyncio.create_task(handle_message(msg))
	except Exception:
		logger.exception("Error while processing Telegram webhook payload.")

	return {"ok": True}


@router.get("/webhook/whatsapp")
async def verify_whatsapp_webhook(
	hub_mode: str | None = Query(default=None, alias="hub.mode"),
	hub_verify_token: str | None = Query(default=None, alias="hub.verify_token"),
	hub_challenge: int | None = Query(default=None, alias="hub.challenge"),
) -> Response:
	try:
		if hub_mode == "subscribe" and hub_verify_token == WEBHOOK_VERIFY_TOKEN and hub_challenge is not None:
			return Response(content=str(hub_challenge), media_type="text/plain", status_code=200)
	except Exception:
		logger.exception("Unexpected error while verifying WhatsApp webhook.")

	return Response(content='{"status":"ok"}', media_type="application/json", status_code=200)


@router.post("/webhook/whatsapp")
async def whatsapp_webhook(request: Request) -> dict[str, str]:
	try:
		data: dict[str, Any] = await request.json()

		entry = data.get("entry")
		if not isinstance(entry, list) or not entry:
			return {"status": "ok"}

		changes = entry[0].get("changes") if isinstance(entry[0], dict) else None
		if not isinstance(changes, list) or not changes:
			return {"status": "ok"}

		value = changes[0].get("value") if isinstance(changes[0], dict) else None
		if not isinstance(value, dict):
			return {"status": "ok"}

		messages = value.get("messages")
		if not isinstance(messages, list) or not messages:
			return {"status": "ok"}

		message = messages[0]
		if not isinstance(message, dict):
			return {"status": "ok"}

		phone = message.get("from")
		text_data = message.get("text")
		text = text_data.get("body") if isinstance(text_data, dict) else None

		if isinstance(phone, str) and isinstance(text, str) and phone and text:
			msg = BotMessage(platform=Platform.WHATSAPP, chat_id=phone, text=text)
			asyncio.create_task(handle_message(msg))
	except Exception:
		logger.exception("Error while processing WhatsApp webhook payload.")

	return {"status": "ok"}
