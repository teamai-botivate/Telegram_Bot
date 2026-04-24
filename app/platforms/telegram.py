from __future__ import annotations

import logging
import os

import httpx
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_API_TEMPLATE = "https://api.telegram.org/bot{token}/{method}"
TELEGRAM_MAX_TEXT_LENGTH = 4096


class TelegramSendError(Exception):
    """Raised when sending a Telegram message fails."""


def _build_telegram_url(method: str) -> str:
    return TELEGRAM_API_TEMPLATE.format(token=TELEGRAM_BOT_TOKEN, method=method)


def _chunk_text(text: str, chunk_size: int = TELEGRAM_MAX_TEXT_LENGTH) -> list[str]:
    if text == "":
        return [""]
    return [text[index : index + chunk_size] for index in range(0, len(text), chunk_size)]


async def send_typing(chat_id: str) -> None:
    if not TELEGRAM_BOT_TOKEN:
        raise TelegramSendError("TELEGRAM_BOT_TOKEN is not configured in .env.")

    url = _build_telegram_url("sendChatAction")
    payload = {"chat_id": chat_id, "action": "typing"}

    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            response = await client.post(url, json=payload)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            logger.error("Telegram typing indicator failed: %s", str(exc))
            raise TelegramSendError("Failed to send typing indicator to Telegram.") from exc


async def send_message(chat_id: str, text: str) -> None:
    if not TELEGRAM_BOT_TOKEN:
        raise TelegramSendError("TELEGRAM_BOT_TOKEN is not configured in .env.")

    url = _build_telegram_url("sendMessage")
    chunks = _chunk_text(text)

    async with httpx.AsyncClient(timeout=30.0) as client:
        for index, chunk in enumerate(chunks, start=1):
            payload = {
                "chat_id": chat_id,
                "text": chunk,
            }

            try:
                response = await client.post(url, json=payload)
                logger.info(
                    "Telegram send chunk %s/%s status=%s",
                    index,
                    len(chunks),
                    response.status_code,
                )
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                logger.error(
                    "Telegram API error chunk %s/%s status=%s body=%s",
                    index,
                    len(chunks),
                    exc.response.status_code,
                    exc.response.text,
                )
                raise TelegramSendError("Failed to send Telegram message.") from exc
            except httpx.HTTPError as exc:
                logger.error(
                    "Telegram request error chunk %s/%s: %s",
                    index,
                    len(chunks),
                    str(exc),
                )
                raise TelegramSendError("Failed to send Telegram message.") from exc
