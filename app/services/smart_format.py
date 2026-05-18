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
            result = await _llm_format(company_name, question, sql_results)
            record_summary_pattern(shape, "llm")
            return result

        record_summary_pattern(shape, "template")
        return result

    # ── LLM path (for medium/large/complex results) ──────────────────────
    result = await _llm_format(company_name, question, sql_results)
    record_summary_pattern(shape, "llm")
    return result


async def _llm_format(
    company_name: str,
    question: str,
    sql_results: list[dict[str, Any]],
) -> str:
    """Format results using the fast LLM model."""
    total_rows = len(sql_results)
    display_rows = sql_results[:500]

    truncation_rule = (
        f"- The data has been truncated. You MUST add a note at the end saying: 'Showing {len(display_rows)} out of {total_rows} records.'"
        if total_rows > len(display_rows)
        else "- DO NOT add any note like 'Showing X of Y records' or 'Showing all records'. Just list the data."
    )

    system_prompt = f"""Format the following database query results for the user.
You are {company_name}'s data assistant answering via WhatsApp and Telegram.
Language: ONLY English. Do not include any text in other languages.

User Question: "{question}"
Data (first {len(display_rows)} of {total_rows} rows):
{json.dumps(display_rows, default=str)}

FORMATTING RULES FOR WHATSAPP/TELEGRAM:
- Make it conversational, clear, and easy to read on a mobile phone.
- Use emojis appropriately but sparingly to make it look professional yet friendly.
- Present lists as clean bullet points (-) or numbered lists (1., 2., 3.).
- PLAIN TEXT ONLY. ABSOLUTELY NO MARKDOWN. Do NOT use asterisks (*) for bold or italics. Do not use **text**.
- Skip completely empty or blank entries (e.g., if a row only has null or empty names, do not list it as '1. .' or 'Empty').
- DO NOT dump raw JSON keys (like "ID: 1, Employee_ID: null, Name: null"). Extract the actual human-readable meaning and present it nicely (e.g., "• John Doe"). Ignore completely null or irrelevant internal database IDs unless specifically asked.
{truncation_rule}
- Single counts should be stated conversationally: "There are 835 pending tasks."
- Do not explain your thought process. Just provide the final formatted message."""

    return await _call_openai_formatting(system_prompt, question, max_tokens=3000)
