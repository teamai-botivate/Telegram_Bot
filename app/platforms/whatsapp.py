"""WhatsApp Cloud API sender.

Posts text messages to the Graph API. Mirrors the Telegram sender's behavior:
- Splits long replies into ~4096-char chunks (WhatsApp's body limit).
- Logs per-chunk status code.
- Raises on non-2xx so the caller's exception handling kicks in.

Notes on the 24-hour customer service window:
WhatsApp only allows free-form (non-template) messages within 24 hours of the
user's last message to your number. Since every reply we send is in response to
an inbound message, we're always inside that window — no template messages
needed for the bot use case.
"""
from __future__ import annotations

import logging
import os

import httpx
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN", "")
WHATSAPP_PHONE_NUMBER_ID = os.getenv("WHATSAPP_PHONE_NUMBER_ID", "")
WHATSAPP_API_VERSION = os.getenv("WHATSAPP_API_VERSION", "v22.0")
WHATSAPP_MAX_TEXT_LENGTH = 4096


def _chunk_text(text: str, chunk_size: int = WHATSAPP_MAX_TEXT_LENGTH) -> list[str]:
    if text == "":
        return [""]
    return [text[index : index + chunk_size] for index in range(0, len(text), chunk_size)]


async def send_message(to: str, text: str) -> None:
    """Send a text message to a WhatsApp user.

    `to` is the recipient phone number in E.164 format without the leading "+"
    (e.g. "919876543210"). That's the same shape Meta delivers in the inbound
    webhook's `from` field, so the bot can simply echo it back.
    """
    if not WHATSAPP_TOKEN or not WHATSAPP_PHONE_NUMBER_ID:
        raise RuntimeError(
            "WHATSAPP_TOKEN and WHATSAPP_PHONE_NUMBER_ID must be configured in .env."
        )

    url = f"https://graph.facebook.com/{WHATSAPP_API_VERSION}/{WHATSAPP_PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json",
    }

    chunks = _chunk_text(text)
    async with httpx.AsyncClient(timeout=30.0) as client:
        for i, chunk in enumerate(chunks, start=1):
            payload = {
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": to,
                "type": "text",
                "text": {"body": chunk, "preview_url": False},
            }
            response = await client.post(url, headers=headers, json=payload)
            logger.info(
                "WhatsApp send chunk %d/%d to=%s status=%s",
                i, len(chunks), to, response.status_code,
            )

            if response.status_code >= 400:
                # Surface Meta's error body — usually {"error": {"message", "code", ...}}.
                # We log a single ERROR line and return cleanly rather than raising,
                # because most 4xx errors are user-side (recipient not allowed,
                # 24h window closed, blocked number) and shouldn't pollute logs
                # with a stack trace on every occurrence. 5xx still raises so the
                # caller can decide whether to retry.
                body_snippet = response.text[:500]
                logger.error(
                    "WhatsApp send failed status=%s to=%s body=%s",
                    response.status_code, to, body_snippet,
                )
                if 400 <= response.status_code < 500:
                    # Stop sending further chunks — they'll all fail with the
                    # same client-side reason. Return cleanly.
                    return
                response.raise_for_status()
