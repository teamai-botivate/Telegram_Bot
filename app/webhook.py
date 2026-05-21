import asyncio
import logging
import os
from collections import OrderedDict
from typing import Any

from dotenv import load_dotenv
from fastapi import APIRouter, Query, Request, Response

from .bot_logic import handle_adddb_callback, handle_message
from .platforms.base import BotMessage, Platform

load_dotenv()

logger = logging.getLogger(__name__)
WEBHOOK_VERIFY_TOKEN = os.getenv("WEBHOOK_VERIFY_TOKEN", "")

router = APIRouter(tags=["webhook"])

# ── Webhook deduplication ────────────────────────────────────────────────────
# Telegram occasionally redelivers the same update_id (e.g. during Render free-tier
# wake-ups or rolling deploys). Track recent update_ids in memory and drop dupes.
# WhatsApp messages have a unique `id` field we use for the same purpose.
_RECENT_UPDATE_IDS: "OrderedDict[str, None]" = OrderedDict()
_RECENT_UPDATE_IDS_MAX = 1000


def _is_duplicate_update(key: str) -> bool:
	"""Return True if this update_id was seen recently. Records the key as seen."""
	if key in _RECENT_UPDATE_IDS:
		# Move to end so it stays "hot" in the LRU.
		_RECENT_UPDATE_IDS.move_to_end(key)
		return True
	_RECENT_UPDATE_IDS[key] = None
	while len(_RECENT_UPDATE_IDS) > _RECENT_UPDATE_IDS_MAX:
		_RECENT_UPDATE_IDS.popitem(last=False)
	return False


@router.post("/webhook/telegram")
async def telegram_webhook(request: Request) -> dict[str, bool]:
	try:
		data: dict[str, Any] = await request.json()

		# Drop duplicate deliveries. Telegram guarantees update_id is unique per update,
		# but the same update can be redelivered (Render wake-ups, rolling deploys, etc).
		update_id = data.get("update_id")
		if update_id is not None:
			dedup_key = f"tg:{update_id}"
			if _is_duplicate_update(dedup_key):
				logger.info("[WEBHOOK] Dropping duplicate Telegram update_id=%s", update_id)
				return {"ok": True}

		# Handle inline keyboard button presses
		callback_query = data.get("callback_query")
		if isinstance(callback_query, dict):
			cq_id = str(callback_query.get("id", ""))
			cq_data = callback_query.get("data", "")
			cq_from = callback_query.get("from") or {}
			cq_message = callback_query.get("message") or {}
			cq_chat = cq_message.get("chat") or {}
			cq_chat_id = cq_chat.get("id") or cq_from.get("id")
			if cq_chat_id and isinstance(cq_data, str) and cq_data.startswith("adddb_product:"):
				asyncio.create_task(
					handle_adddb_callback(
						chat_id=str(cq_chat_id),
						callback_query_id=cq_id,
						callback_data=cq_data,
					)
				)
			return {"ok": True}

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

		# If the user replied-to / quoted an earlier message, capture its text
		# so the pipeline can resolve "tell me about this" style follow-ups.
		reply_to_text: str | None = None
		reply_to_message = message.get("reply_to_message")
		if isinstance(reply_to_message, dict):
			rt_text = reply_to_message.get("text") or reply_to_message.get("caption")
			if isinstance(rt_text, str) and rt_text.strip():
				# Trim defensively — keep the prompt cheap.
				reply_to_text = rt_text.strip()[:1500]

		msg = BotMessage(
			platform=Platform.TELEGRAM,
			chat_id=str(chat_id_value),
			text=text,
			reply_to_text=reply_to_text,
		)
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

		# Drop duplicate WhatsApp deliveries by message id.
		wa_message_id = message.get("id")
		if wa_message_id is not None:
			dedup_key = f"wa:{wa_message_id}"
			if _is_duplicate_update(dedup_key):
				logger.info("[WEBHOOK] Dropping duplicate WhatsApp message id=%s", wa_message_id)
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
