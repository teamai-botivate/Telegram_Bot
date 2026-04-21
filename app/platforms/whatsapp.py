from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()

WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN", "")
WHATSAPP_PHONE_NUMBER_ID = os.getenv("WHATSAPP_PHONE_NUMBER_ID", "")
WHATSAPP_MAX_TEXT_LENGTH = 4096


def _chunk_text(text: str, chunk_size: int = WHATSAPP_MAX_TEXT_LENGTH) -> list[str]:
    if text == "":
        return [""]
    return [text[index : index + chunk_size] for index in range(0, len(text), chunk_size)]


async def send_message(to: str, text: str) -> None:
    _ = (to, text)
    raise NotImplementedError(
        "WhatsApp credentials not yet configured. See README for setup instructions."
    )

    # Full implementation ready to activate once credentials are configured:
    # import logging
    # import httpx
    #
    # logger = logging.getLogger(__name__)
    #
    # if not WHATSAPP_TOKEN or not WHATSAPP_PHONE_NUMBER_ID:
    #     raise RuntimeError("WHATSAPP_TOKEN and WHATSAPP_PHONE_NUMBER_ID must be configured in .env.")
    #
    # url = f"https://graph.facebook.com/v19.0/{WHATSAPP_PHONE_NUMBER_ID}/messages"
    # headers = {
    #     "Authorization": f"Bearer {WHATSAPP_TOKEN}",
    #     "Content-Type": "application/json",
    # }
    #
    # chunks = _chunk_text(text)
    # async with httpx.AsyncClient(timeout=30.0) as client:
    #     for chunk in chunks:
    #         payload = {
    #             "messaging_product": "whatsapp",
    #             "recipient_type": "individual",
    #             "to": to,
    #             "type": "text",
    #             "text": {"body": chunk},
    #         }
    #         response = await client.post(url, headers=headers, json=payload)
    #         logger.info("WhatsApp send status=%s", response.status_code)
    #         response.raise_for_status()
