from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.platforms.base import BotMessage

# ── In-memory conversation history ───────────────────────────────────────────

_conversation_context: dict[str, list[dict[str, Any]]] = {}

MAX_CONVERSATION_CONTEXT_ITEMS = 5


def _context_key(msg: BotMessage) -> str:
    return f"{msg.platform.value}:{msg.chat_id}"


def _build_conversation_context_block(msg: BotMessage) -> str:
    history = _conversation_context.get(_context_key(msg), [])
    quoted = getattr(msg, "reply_to_text", None)

    if not history and not quoted:
        return ""

    lines: list[str] = []

    # An explicit Telegram reply-to quote is the strongest possible signal —
    # surface it prominently so the LLM resolves "this", "that", "the last one".
    if quoted:
        lines.append(
            "USER IS REPLYING TO THIS EARLIER MESSAGE — treat it as the subject "
            "of the current question whenever the current question is ambiguous:"
        )
        lines.append(f'"""{quoted}"""')
        lines.append("")

    if history:
        lines.append("RECENT CHAT CONTEXT (use only when the current question is a follow-up):")
        for index, item in enumerate(history[-MAX_CONVERSATION_CONTEXT_ITEMS:], start=1):
            lines.append(f"{index}. User: {item.get('question', '')}")
            if item.get("sql"):
                lines.append(f"   SQL: {item['sql']}")
            if item.get("reply"):
                lines.append(f"   Assistant: {item['reply']}")
        lines.append(
            "If the current question is short or elliptical, inherit relevant table/filter/status constraints from this context. "
            "If the current question clearly changes scope, follow the current question."
        )

    return "\n".join(lines)


def _remember_conversation_context(
    msg: BotMessage,
    question: str,
    reply: str,
    sql: str | None = None,
) -> None:
    key = _context_key(msg)
    history = _conversation_context.setdefault(key, [])
    history.append(
        {
            "question": question,
            "reply": reply,
            "sql": sql,
            "stored_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    del history[:-MAX_CONVERSATION_CONTEXT_ITEMS]
