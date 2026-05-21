"""Smart response formatting: template responses for simple results,
fast LLM for complex results. Tracks patterns for continuous improvement.
"""
from __future__ import annotations

import json
from typing import Any

from .core import logger
from .llm import _call_openai_formatting
from .runtime_memory import classify_result_shape, should_use_template, record_summary_pattern


# ── Template Responses (zero-LLM, instant) ───────────────────────────────────

def _format_single_count(question: str, rows: list[dict[str, Any]]) -> str:
    """Format a single numeric result (e.g., COUNT queries)."""
    val = list(rows[0].values())[0]
    col_name = list(rows[0].keys())[0]

    # Make column name human-readable
    label = col_name.replace("_", " ").replace("count", "").strip()
    if not label:
        label = "records"

    # Conversational response
    return f"There are {val} {label}."


def _format_single_row(question: str, rows: list[dict[str, Any]]) -> str:
    """Format a single row result with multiple columns."""
    row = rows[0]
    lines: list[str] = []
    for key, value in row.items():
        if value is None or str(value).strip() == "":
            continue
        label = key.replace("_", " ").title()
        lines.append(f"• {label}: {value}")

    if not lines:
        return "No data found for your query."

    return "\n".join(lines)


def _format_short_list(question: str, rows: list[dict[str, Any]]) -> str:
    """Format 2-5 rows as a clean numbered list."""
    if not rows:
        return "No results found."

    # Determine the best column to use as the primary display
    keys = list(rows[0].keys())
    lines: list[str] = []

    for i, row in enumerate(rows, start=1):
        values = [str(v) for v in row.values() if v is not None and str(v).strip()]
        if len(values) <= 3:
            # Compact display
            lines.append(f"{i}. {' | '.join(values)}")
        else:
            # First 2-3 key values, then count of remaining
            primary = " | ".join(values[:3])
            lines.append(f"{i}. {primary}")

    return "\n".join(lines)


def _format_empty(question: str) -> str:
    return "I couldn't find any data matching your request."


def _format_fallback_bullets(rows: list[dict[str, Any]]) -> str:
    """Last-resort formatter used when the LLM formatter is unavailable
    (rate-limited, payload too large, network error). Picks the most
    human-meaningful field per row and renders a bullet list.
    """
    if not rows:
        return "No results found."

    PREFERRED_FIELDS = (
        "title", "name", "full_name", "company_name", "contact_name",
        "subject", "description", "label", "product_name", "task",
    )

    lines: list[str] = []
    total = len(rows)
    shown = min(total, 20)
    for i, row in enumerate(rows[:shown], start=1):
        # Pick the first preferred field that's non-empty
        primary = None
        for key in PREFERRED_FIELDS:
            v = row.get(key)
            if isinstance(v, str) and v.strip():
                primary = v.strip()
                break
        if primary is None:
            # Fall back to the first non-id, non-null value
            for k, v in row.items():
                if k in ("id",) or (isinstance(k, str) and k.endswith("_id")):
                    continue
                if v is None or (isinstance(v, str) and not v.strip()):
                    continue
                primary = str(v).strip()
                break
        if primary is None:
            continue
        # Truncate very long values
        if len(primary) > 200:
            primary = primary[:200].rstrip() + "…"
        lines.append(f"{i}. {primary}")

    if not lines:
        return "I found results but couldn't format them right now. Please try a more specific question."

    suffix = f"\n\nShowing {shown} of {total} records." if total > shown else ""
    return "Here's what I found:\n" + "\n".join(lines) + suffix


# ── Smart Format Dispatcher ─────────────────────────────────────────────────

async def smart_format_response(
    company_name: str,
    question: str,
    sql_results: list[dict[str, Any]],
) -> str:
    """Intelligently format SQL results — use templates for simple results,
    LLM for complex ones. Tracks patterns to improve over time.

    Returns the formatted response string.
    """
    shape = classify_result_shape(sql_results)
    use_template = should_use_template(shape)

    logger.debug("[FORMAT] shape=%s use_template=%s rows=%d", shape, use_template, len(sql_results))

    # ── Template path (instant, no LLM cost) ─────────────────────────────
    if use_template:
        if shape == "empty":
            result = _format_empty(question)
        elif shape == "single_count":
            result = _format_single_count(question, sql_results)
        elif shape == "single_row":
            result = _format_single_row(question, sql_results)
        elif shape == "short_list":
            result = _format_short_list(question, sql_results)
        else:
            # Fallback to LLM for shapes we don't have templates for
            result = await _safe_llm_format(company_name, question, sql_results)
            record_summary_pattern(shape, "llm")
            return result

        record_summary_pattern(shape, "template")
        return result

    # ── LLM path (for medium/large/complex results) ──────────────────────
    result = await _safe_llm_format(company_name, question, sql_results)
    record_summary_pattern(shape, "llm")
    return result


async def _safe_llm_format(
    company_name: str,
    question: str,
    sql_results: list[dict[str, Any]],
) -> str:
    """Call the LLM formatter; on any error (rate limit, 413, timeout), fall back
    to a basic bullet-list template so the user still gets useful info.
    """
    try:
        return await _llm_format(company_name, question, sql_results)
    except Exception as exc:
        logger.warning(
            "[FORMAT_FALLBACK] LLM formatter failed (%s); using bullet fallback for %d rows",
            exc, len(sql_results),
        )
        return _format_fallback_bullets(sql_results)


async def _llm_format(
    company_name: str,
    question: str,
    sql_results: list[dict[str, Any]],
) -> str:
    """Format results using the fast LLM model.

    Wide tables (articles with body text, long descriptions, etc.) can blow past
    the fast LLM's per-minute token budget. We apply two safeguards:
      1. Truncate any single text cell to MAX_CELL_CHARS chars.
      2. Apply an overall payload budget: stop adding rows once we've used MAX_TOTAL_CHARS.
    """
    MAX_CELL_CHARS = 400          # Per-field cap (excerpts, body, etc.)
    MAX_TOTAL_CHARS = 12_000      # ~3k tokens of row data — leaves room for prompt
    SAMPLE_RATIO = 4              # Keep 1 row per N when over budget

    total_rows = len(sql_results)
    display_rows: list[dict[str, Any]] = []
    used_chars = 0
    truncated_for_size = False

    for row in sql_results[:500]:
        compact: dict[str, Any] = {}
        for k, v in row.items():
            if isinstance(v, str) and len(v) > MAX_CELL_CHARS:
                compact[k] = v[:MAX_CELL_CHARS] + "…"
            else:
                compact[k] = v
        row_chars = len(json.dumps(compact, default=str))
        if used_chars + row_chars > MAX_TOTAL_CHARS and display_rows:
            truncated_for_size = True
            break
        display_rows.append(compact)
        used_chars += row_chars

    truncation_rule = (
        f"- The data has been truncated. You MUST add a note at the end saying: 'Showing {len(display_rows)} out of {total_rows} records.'"
        if total_rows > len(display_rows) or truncated_for_size
        else "- DO NOT add any note like 'Showing X of Y records' or 'Showing all records'. Just list the data."
    )

    system_prompt = f"""You are {company_name}'s data assistant answering via WhatsApp and Telegram.
Your job is to turn raw database results into a clear, helpful, human-readable message.

User Question: "{question}"
Data ({len(display_rows)} of {total_rows} rows):
{json.dumps(display_rows, default=str)}

FORMATTING RULES:
- PLAIN TEXT ONLY. No markdown, no asterisks (*) for bold, no **text**.
- Language: ONLY English.
- Make it conversational and easy to read on a mobile phone.
- If the question is about a table's structure or schema (columns, data types, what it stores):
  Describe what the table is used for in one sentence, then list each column with a plain-English explanation of what it holds. Example: "The ai_tasks table stores AI-generated tasks. It has 5 columns: id - unique task ID, description - task details, timestamp - when the task was created, planned_date - scheduled date, department - the team it belongs to."
- If the data is a list of records, use a numbered list. Skip null/empty rows entirely.
- DO NOT dump raw JSON keys verbatim. Translate column names into plain English labels.
- Ignore internal IDs (like row id) unless the user specifically asked for them.
- Single counts: state conversationally, e.g. "There are 835 pending tasks."
- Use emojis sparingly only when they genuinely add clarity.
{truncation_rule}
- Do not explain your reasoning. Output only the final message."""

    return await _call_openai_formatting(system_prompt, question, max_tokens=3000)
