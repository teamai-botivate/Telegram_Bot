from __future__ import annotations
import json
from typing import Any

from .llm import _call_openai_formatting

async def format_sql_response(company_name: str, question: str, sql_results: list[dict[str, Any]]) -> str:
    total_rows = len(sql_results)
    # Increase from 100 to 500 to let the user see practically all records
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
