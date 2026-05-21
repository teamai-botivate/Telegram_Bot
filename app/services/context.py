from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from app.platforms.base import BotMessage

# ── In-memory conversation history ───────────────────────────────────────────

_conversation_context: dict[str, list[dict[str, Any]]] = {}

MAX_CONVERSATION_CONTEXT_ITEMS = 5
# Cap rows we keep per turn to avoid blowing up prompt size; 10 covers most
# "show me N items" follow-ups.
MAX_RESULT_ROWS_IN_CONTEXT = 10


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
            # Only surface raw result rows when the user is explicitly replying-to
            # a prior message. For normal follow-ups, the question text + prior SQL
            # are enough — adding rows on every turn would inflate prompt size for
            # no real benefit.
            if quoted:
                rows = item.get("result_rows")
                if rows:
                    lines.append(
                        f"   Result rows (in original order, first {len(rows)}): "
                        f"{json.dumps(rows, default=str)}"
                    )
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
    result_rows: list[dict[str, Any]] | None = None,
) -> None:
    key = _context_key(msg)
    history = _conversation_context.setdefault(key, [])

    # Trim result rows so the prompt doesn't explode on big result sets.
    trimmed_rows: list[dict[str, Any]] | None = None
    if result_rows:
        trimmed_rows = []
        for row in result_rows[:MAX_RESULT_ROWS_IN_CONTEXT]:
            # Drop verbose columns (IDs, embeddings) that don't help disambiguation;
            # keep human-meaningful fields the user is likely to reference.
            compact = {
                k: v for k, v in row.items()
                if not (isinstance(k, str) and (k.endswith("_id") or k == "id" or k.endswith("_embedding")))
            }
            trimmed_rows.append(compact)

    history.append(
        {
            "question": question,
            "reply": reply,
            "sql": sql,
            "result_rows": trimmed_rows,
            "stored_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    del history[:-MAX_CONVERSATION_CONTEXT_ITEMS]
