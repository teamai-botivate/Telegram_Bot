from __future__ import annotations

import re

from .core import (
    AsyncOpenAI,
    OPENAI_API_KEY,
    FAST_LLM_MODEL,
    FAST_LLM_PROVIDER,
    FAST_LLM_API_KEY,
    FAST_LLM_BASE_URL,
    RESPONSE_FORMAT_MODEL,
    SQL_GENERATION_MODEL,
    OFF_TOPIC_CLASSIFIER_MODEL,
    EXAMPLES_LLM_PROVIDER,
    EXAMPLES_LLM_API_KEY,
    EXAMPLES_LLM_MODEL,
    EXAMPLES_LLM_BASE_URL,
    _openai_import_error,
    _openai_client,
    _fast_llm_client,
    logger,
)

# ── OpenAI client factory (main model — SQL generation) ──────────────────────

def _get_openai_client() -> AsyncOpenAI:
    from . import core  # mutable access to the module-level singleton

    if _openai_import_error is not None:
        raise RuntimeError(
            "openai package is not installed. "
            "Add it to environment with pip install -r requirements.txt."
        )
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY is not configured in .env.")
    if core._openai_client is None:
        core._openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY)
    return core._openai_client


# ── Fast LLM client factory (Groq / Cerebras / OpenAI) ──────────────────────

def _get_fast_llm_client() -> AsyncOpenAI:
    """Return a client for the fast LLM provider (Groq, Cerebras, or OpenAI).

    When FAST_LLM_PROVIDER=groq or cerebras, creates a separate client with
    the provider's base_url and API key. When provider=openai (default),
    reuses the standard OpenAI client.
    """
    from . import core

    if _openai_import_error is not None:
        raise RuntimeError("openai package is not installed.")

    # If provider is OpenAI, reuse the main client
    if FAST_LLM_PROVIDER == "openai":
        return _get_openai_client()

    # For Groq/Cerebras: need a dedicated API key
    api_key = FAST_LLM_API_KEY
    if not api_key:
        logger.warning(
            "[FAST_LLM] FAST_LLM_API_KEY not set for provider=%s, falling back to OpenAI.",
            FAST_LLM_PROVIDER,
        )
        return _get_openai_client()

    if core._fast_llm_client is None:
        core._fast_llm_client = AsyncOpenAI(
            api_key=api_key,
            base_url=FAST_LLM_BASE_URL,
        )
        logger.info(
            "[FAST_LLM] Initialized %s client: model=%s base_url=%s",
            FAST_LLM_PROVIDER, FAST_LLM_MODEL, FAST_LLM_BASE_URL,
        )

    return core._fast_llm_client


# ── Examples LLM client (independent override; defaults to fast LLM) ────────

def _get_examples_llm_client() -> tuple[AsyncOpenAI, str]:
    """Return (client, model_name) used for generating welcome example questions.

    Resolution order:
      1. If EXAMPLES_LLM_PROVIDER + EXAMPLES_LLM_API_KEY are set, use that
         (e.g. Cerebras for sub-second latency).
      2. Otherwise fall back to the fast LLM (Groq / Cerebras / OpenAI mini).
    """
    from . import core

    if _openai_import_error is not None:
        raise RuntimeError("openai package is not installed.")

    if EXAMPLES_LLM_PROVIDER and EXAMPLES_LLM_API_KEY and EXAMPLES_LLM_MODEL:
        if core._examples_llm_client is None:
            core._examples_llm_client = AsyncOpenAI(
                api_key=EXAMPLES_LLM_API_KEY,
                base_url=EXAMPLES_LLM_BASE_URL or None,
            )
            logger.info(
                "[EXAMPLES_LLM] Initialized %s client: model=%s base_url=%s",
                EXAMPLES_LLM_PROVIDER, EXAMPLES_LLM_MODEL, EXAMPLES_LLM_BASE_URL,
            )
        return core._examples_llm_client, EXAMPLES_LLM_MODEL

    # Fallback — use the fast LLM client and its model.
    return _get_fast_llm_client(), FAST_LLM_MODEL


# ── LLM call wrappers ────────────────────────────────────────────────────────

async def _call_fast_llm(
    system_prompt: str, user_prompt: str, max_tokens: int = 300
) -> str:
    """Call the fast/cheap model (Groq/Cerebras/OpenAI-mini) for intent, routing, formatting."""
    client = _get_fast_llm_client()
    completion = await client.chat.completions.create(
        model=FAST_LLM_MODEL,
        temperature=0,
        max_tokens=max_tokens,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    )
    content = completion.choices[0].message.content
    return (content or "").strip()


async def _call_openai_formatting(
    system_prompt: str, user_prompt: str, max_tokens: int = 600
) -> str:
    """Call the formatting model (uses fast provider — Groq/Cerebras/OpenAI)."""
    client = _get_fast_llm_client()
    completion = await client.chat.completions.create(
        model=RESPONSE_FORMAT_MODEL,
        temperature=0,
        max_tokens=max_tokens,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    )
    content = completion.choices[0].message.content
    return (content or "").strip()


async def _call_openai_sql(system_prompt: str, user_prompt: str) -> str:
    """Call the main/expensive model — used ONLY for SQL generation (always OpenAI)."""
    client = _get_openai_client()
    completion = await client.chat.completions.create(
        model=SQL_GENERATION_MODEL,
        temperature=0,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    )
    content = completion.choices[0].message.content
    return (content or "").strip()


async def _call_openai_classifier(system_prompt: str, user_prompt: str) -> str:
    """Call the classifier model (uses fast provider — Groq/Cerebras/OpenAI)."""
    client = _get_fast_llm_client()
    completion = await client.chat.completions.create(
        model=OFF_TOPIC_CLASSIFIER_MODEL,
        temperature=0,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    )
    content = completion.choices[0].message.content
    return (content or "").strip()


# ── Off-topic heuristic ──────────────────────────────────────────────────────

async def is_off_topic(text: str) -> bool:
    """Fast local heuristic to reject obvious small talk/junk.
    Eliminates a 2-3s LLM round trip. If a junk message slips through,
    the SQL pipeline will safely fail to find data anyway.
    """
    text_lower = text.strip().lower()

    if len(text_lower) < 2:
        return True

    # Common small talk (exact or near-exact match)
    small_talk = {
        "hi", "hello", "hey", "good morning", "good evening", "good afternoon",
        "how are you", "how are you?", "who are you", "who are you?", "what are you",
        "thanks", "thank you", "bye", "goodbye", "ok", "okay", "test", "testing", "ping",
    }
    if text_lower in small_talk:
        return True

    # Common LLM jailbreak / out-of-bounds prefixes
    junk_patterns = [
        r"^tell me a joke",
        r"^what is the weather",
        r"^write a poem",
        r"^write code",
        r"^who is the president",
        r"^how to make",
        r"^recipe for",
        r"^sing a song",
        r"^ignore all previous",
    ]
    if any(re.search(p, text_lower) for p in junk_patterns):
        return True

    return False
