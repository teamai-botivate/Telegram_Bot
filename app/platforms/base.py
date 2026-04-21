from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol


class Platform(str, Enum):
    TELEGRAM = "telegram"
    WHATSAPP = "whatsapp"


@dataclass(slots=True)
class BotMessage:
    platform: Platform
    chat_id: str
    text: str


class Sender(Protocol):
    async def send_message(self, chat_id: str, text: str) -> None:
        ...


async def send_reply(msg: BotMessage, reply_text: str) -> None:
    if msg.platform == Platform.TELEGRAM:
        from .telegram import send_message

        await send_message(msg.chat_id, reply_text)
        return

    if msg.platform == Platform.WHATSAPP:
        from .whatsapp import send_message

        await send_message(msg.chat_id, reply_text)
        return

    raise ValueError(f"Unsupported platform: {msg.platform}")
