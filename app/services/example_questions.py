"""Generate 3-4 example questions tailored to a tenant's actual database schema.

Used in the /start welcome message and the /help reply so suggestions feel
relevant ("Show me articles by category" vs. the generic "How many pending tasks?").

Cached per credential id forever — invalidated only when refresh-schema fires.
Cost: ~1 cheap OpenAI call per credential per schema refresh.
"""

from __future__ import annotations

import json
import re
from typing import Any

from .core import logger, OPENAI_API_KEY, SQL_GENERATION_MODEL
from .llm import _get_openai_client


# ── Cache ────────────────────────────────────────────────────────────────────
# Keyed by credential id (str). Values are list[str] (the example questions).
# In-memory only — cleared on process restart and on refresh-schema.
_example_cache: dict[str, list[str]] = {}


# ── Hardcoded fallback (used when OpenAI is unavailable or blueprint is empty) ─
_GENERIC_FALLBACK: list[str] = [
    "How many records are there?",
    "Show me the latest entries",
    "List everything we have",
    "Give me a summary of the data",
]


def invalidate_example_cache(credential_id: Any | None = None) -> None:
    """Drop cached examples. Pass a credential id to clear one entry, or None for all."""
    if credential_id is not None:
        key = str(credential_id)
        if _example_cache.pop(key, None) is not None:
            logger.info("[EXAMPLES] Cache invalidated for credential=%s", key)
    else:
        _example_cache.clear()
        logger.info("[EXAMPLES] Cache invalidated (all)")


async def generate_example_questions(
    company_name: str,
    schema_blueprint: str | None,
    credential_id: Any | None = None,
    count: int = 4,
) -> list[str]:
    """Return up to `count` natural-language example questions tailored to the schema.

    Uses an in-memory cache keyed by credential_id. On any LLM/parse error, returns
    a generic fallback list rather than raising.
    """
    cache_key = str(credential_id) if credential_id is not None else None
    if cache_key and cache_key in _example_cache:
        return _example_cache[cache_key][:count]

    if not schema_blueprint or not schema_blueprint.strip():
        return _GENERIC_FALLBACK[:count]

    if not OPENAI_API_KEY:
        logger.debug("[EXAMPLES] OPENAI_API_KEY not set — using generic fallback.")
        return _GENERIC_FALLBACK[:count]

    # Keep prompt small — schema blueprints can be large, but we only need
    # a rough sense of the tables/columns to pick natural example questions.
    blueprint_snippet = schema_blueprint[:4000]

    system_prompt = (
        f"You are a helpful onboarding assistant for {company_name}'s data bot. "
        f"Given a database schema, suggest {count} short, friendly natural-language "
        "questions a non-technical business user could ask about this data. "
        "Questions must be:\n"
        "- Natural human English, not SQL.\n"
        "- Specific to the actual tables/columns in this schema (not generic).\n"
        "- Useful for first-time users exploring what they can ask.\n"
        "- Short (under 10 words each).\n"
        "- Varied: mix counts, lists, lookups, filters.\n\n"
        f"Return ONLY valid JSON of this shape: "
        f'{{"questions": ["...", "...", "...", "..."]}}'
    )

    user_prompt = f"DATABASE SCHEMA:\n{blueprint_snippet}\n\nGenerate {count} example questions."

    try:
        client = _get_openai_client()
        completion = await client.chat.completions.create(
            model=SQL_GENERATION_MODEL,
            temperature=0.4,
            max_tokens=400,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        )
        raw = completion.choices[0].message.content or "{}"
        parsed = json.loads(raw)
        questions_raw = parsed.get("questions", [])
        if not isinstance(questions_raw, list):
            raise ValueError("'questions' is not a list")

        questions: list[str] = []
        for q in questions_raw:
            if not isinstance(q, str):
                continue
            q = q.strip().rstrip("?") + "?"
            # Strip leading numbering or bullets the model sometimes adds
            q = re.sub(r"^[\s\-•*\d.\)]+", "", q).strip()
            if 3 <= len(q) <= 120:
                questions.append(q)

        if not questions:
            raise ValueError("no usable questions returned")

        questions = questions[:count]
        if cache_key:
            _example_cache[cache_key] = questions
        logger.info(
            "[EXAMPLES] Generated %d questions for credential=%s", len(questions), cache_key,
        )
        return questions

    except Exception as exc:
        logger.warning("[EXAMPLES] Generation failed (%s); using generic fallback.", exc)
        return _GENERIC_FALLBACK[:count]
