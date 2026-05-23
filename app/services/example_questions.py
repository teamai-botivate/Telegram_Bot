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

from .core import logger
from .llm import _get_examples_llm_client


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

    # Keep prompt small — schema blueprints can be large, but we only need
    # a rough sense of the tables/columns to pick natural example questions.
    blueprint_snippet = schema_blueprint[:4000]

    # Build an explicit JSON skeleton so the model sees exactly how many items
    # we want, with placeholder slots. This drastically improves compliance,
    # especially for reasoning-style models like gpt-oss-120b that otherwise
    # truncate mid-array when max_tokens is tight.
    skeleton_items = ", ".join([f'"question {i + 1}"' for i in range(count)])
    json_skeleton = f'{{"questions": [{skeleton_items}]}}'

    system_prompt = (
        f"You are a helpful onboarding assistant for {company_name}'s data bot. "
        f"Given a database schema, suggest EXACTLY {count} short, friendly "
        "natural-language questions a non-technical business user could ask "
        "about this data.\n\n"
        "GROUNDING RULES — these are mandatory:\n"
        "- Use ONLY the table names and column names that appear in the schema. "
        "Do not invent tables or columns.\n"
        "- DO NOT invent example values. Never substitute placeholder names "
        "('John Doe', 'Acme Corp', 'example@email.com'), placeholder amounts "
        "('$10k', '5 days', '100 units'), placeholder currencies (always omit "
        "the currency symbol), or any other fabricated specific value.\n"
        "- Phrase questions using the COLUMN NAME as the variable, not a "
        "made-up value. Examples of the right style:\n"
        "    ✓ 'Show me the tasks assigned to a person.' — leaves the name open.\n"
        "    ✓ 'List parties by pending collection amount.' — no threshold.\n"
        "    ✓ 'What is the latest expected payment date per party?' — uses real column.\n"
        "    ✗ 'How many tasks did John Doe complete?' — invented name.\n"
        "    ✗ 'List parties with collections over $10k.' — invented threshold + currency.\n"
        "\n"
        "STYLE RULES:\n"
        "- Natural human English, not SQL.\n"
        "- Short (under 10 words each).\n"
        "- Varied: mix counts, lists, lookups, filters, summaries.\n"
        "- Useful for first-time users exploring what they can ask.\n"
        "\n"
        f"You MUST return exactly {count} questions as a JSON object. Output "
        "ONLY the JSON object — no prose, no markdown fences, no commentary.\n"
        f"Shape: {json_skeleton}"
    )

    user_prompt = (
        f"DATABASE SCHEMA:\n{blueprint_snippet}\n\n"
        f"Generate EXACTLY {count} example questions in the required JSON format. "
        f"The 'questions' array MUST contain exactly {count} items. "
        "Remember: use ONLY column names from the schema above; never invent "
        "example values, names, amounts, or thresholds."
    )

    raw = ""
    model = "<unknown>"
    try:
        client, model = _get_examples_llm_client()
        # 800 tokens is enough for ~5 short questions + JSON wrapping + any
        # reasoning overhead the model uses internally. Smaller budgets caused
        # gpt-oss-120b to truncate mid-array.
        completion = await client.chat.completions.create(
            model=model,
            temperature=0.4,
            max_tokens=800,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        )
        raw = (completion.choices[0].message.content or "").strip()

        # Strip common wrappers: ```json ... ``` fences, leading "Here is..." prose
        cleaned = raw
        # Remove markdown fences.
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE).strip()
        cleaned = re.sub(r"\s*```$", "", cleaned).strip()

        questions: list[str] = []

        # Strategy 1: parse the cleaned text as JSON directly.
        try:
            parsed = json.loads(cleaned)
            questions_raw = parsed.get("questions", []) if isinstance(parsed, dict) else parsed
        except Exception:
            # Strategy 2: extract the OUTERMOST JSON object by balanced-brace scan.
            questions_raw = []
            obj_text = _extract_outer_json_object(cleaned)
            if obj_text:
                try:
                    parsed = json.loads(obj_text)
                    if isinstance(parsed, dict):
                        questions_raw = parsed.get("questions", [])
                except Exception:
                    pass

            # Strategy 3 (last resort): pull any quoted strings that look like questions.
            if not questions_raw:
                questions_raw = re.findall(r'"([^"\n]{8,120}\?)"', cleaned)

        if not isinstance(questions_raw, list):
            raise ValueError(f"'questions' is not a list, got: {type(questions_raw).__name__}")

        for q in questions_raw:
            if not isinstance(q, str):
                continue
            q = q.strip().rstrip("?") + "?"
            q = re.sub(r"^[\s\-•*\d.\)]+", "", q).strip()
            if 3 <= len(q) <= 120 and not _looks_invented(q):
                questions.append(q)

        if not questions:
            raise ValueError(f"no usable questions parsed; raw[:300]={raw[:300]!r}")

        # If the model returned fewer items than requested, log it explicitly so
        # we can diagnose. We still return what we got rather than failing —
        # something is better than the generic fallback.
        if len(questions) < count:
            logger.warning(
                "[EXAMPLES] Model returned %d/%d questions (model=%s); "
                "consider raising max_tokens or sharpening the prompt. raw_tail=%r",
                len(questions), count, model, raw[-300:],
            )

        questions = questions[:count]
        if cache_key:
            _example_cache[cache_key] = questions
        logger.info(
            "[EXAMPLES] Generated %d questions for credential=%s model=%s",
            len(questions), cache_key, model,
        )
        return questions

    except Exception as exc:
        # Surface the raw model output so we can diagnose silent failures.
        logger.warning(
            "[EXAMPLES] Generation failed (model=%s err=%s); raw=%r; using generic fallback.",
            model, exc, raw[:500] if raw else "<empty>",
        )
        return _GENERIC_FALLBACK[:count]


def _extract_outer_json_object(text: str) -> str | None:
    """Find the outermost {...} block via balanced-brace scan.

    Handles cases where the LLM wraps the JSON in prose. Returns None if no
    balanced object is found.
    """
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if escape:
            escape = False
            continue
        if ch == "\\" and in_string:
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


# ── Defense-in-depth: reject questions that contain invented values ────────
# These patterns catch the most common LLM hallucinations even when the prompt
# rules above are honored loosely. Schema-agnostic — works for any tenant.
_INVENTED_PATTERNS = [
    # Currency amounts: $100, ₹10k, €50, USD 1000, 50 USD, etc.
    re.compile(r"[\$€£¥₹]\s*\d", re.IGNORECASE),
    re.compile(r"\b\d+\s*(usd|inr|eur|gbp|jpy|rs|rupees|dollars|euros)\b", re.IGNORECASE),
    # Numeric thresholds with k/m/b suffix: 10k, 5m, 100M, 1B.
    re.compile(r"\b\d+\s*[kmb]\b", re.IGNORECASE),
    # Common placeholder personal names. These are real names too, so this is a
    # heuristic — only reject when paired with a typical "by/from/to <name>"
    # context, which is what the LLM tends to invent.
    re.compile(r"\b(john\s+doe|jane\s+doe|john\s+smith|jane\s+smith)\b", re.IGNORECASE),
    # Common placeholder company names.
    re.compile(r"\b(acme\s+(corp|inc|ltd)|example\s+(corp|inc|ltd|company))\b", re.IGNORECASE),
    # Placeholder email addresses.
    re.compile(r"\b(example|test|user|name)@", re.IGNORECASE),
    # Lorem-ipsum and "abc" filler.
    re.compile(r"\blorem\s+ipsum\b", re.IGNORECASE),
]


def _looks_invented(question: str) -> bool:
    """Return True if the question contains a clearly invented value.

    Defense-in-depth filter that drops questions slipping past the prompt rules.
    Schema-agnostic — works for every tenant.
    """
    for pat in _INVENTED_PATTERNS:
        if pat.search(question):
            logger.debug(
                "[EXAMPLES] Rejected invented-looking question: %r matched %r",
                question, pat.pattern,
            )
            return True
    return False
