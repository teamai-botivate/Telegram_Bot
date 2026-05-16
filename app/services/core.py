from __future__ import annotations

import logging
import os

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# ── OpenAI client import ─────────────────────────────────────────────────────
try:
    from openai import AsyncOpenAI
    _openai_import_error: Exception | None = None
except ImportError as e:  # pragma: no cover
    AsyncOpenAI = None  # type: ignore[assignment,misc]
    _openai_import_error = e

# ── Environment configuration ────────────────────────────────────────────────
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")

# Main LLM — used ONLY for SQL generation (quality-critical, OpenAI only)
SQL_GENERATION_MODEL = os.getenv("SQL_GENERATION_MODEL", "gpt-4.1")

# ── Fast LLM provider (Groq / Cerebras / OpenAI) ────────────────────────────
# Groq and Cerebras offer free tiers with blazing-fast inference on open models.
# Both expose OpenAI-compatible APIs, so we use the same AsyncOpenAI client.
#
# Usage in .env:
#   FAST_LLM_PROVIDER=groq          (or: cerebras, openai)
#   FAST_LLM_API_KEY=gsk_xxx        (Groq key) / csk_xxx (Cerebras key)
#   FAST_LLM_MODEL=llama-3.3-70b-versatile  (Groq) / llama-3.3-70b (Cerebras)
#
# If FAST_LLM_PROVIDER is not set, falls back to OpenAI with gpt-4.1-mini.
FAST_LLM_PROVIDER = os.getenv("FAST_LLM_PROVIDER", "openai").lower()
FAST_LLM_API_KEY = os.getenv("FAST_LLM_API_KEY", "")
FAST_LLM_MODEL = os.getenv("FAST_LLM_MODEL", "")

_PROVIDER_DEFAULTS: dict[str, dict[str, str]] = {
    "groq": {
        "base_url": "https://api.groq.com/openai/v1",
        "model": "llama-3.3-70b-versatile",
    },
    "cerebras": {
        "base_url": "https://api.cerebras.ai/v1",
        "model": "llama-3.3-70b",
    },
    "openai": {
        "base_url": "",  # empty → default OpenAI endpoint
        "model": "gpt-4.1-mini",
    },
}

# Resolve fast model name (use user override or provider default)
if not FAST_LLM_MODEL:
    FAST_LLM_MODEL = _PROVIDER_DEFAULTS.get(FAST_LLM_PROVIDER, {}).get("model", "gpt-4.1-mini")

FAST_LLM_BASE_URL = _PROVIDER_DEFAULTS.get(FAST_LLM_PROVIDER, {}).get("base_url", "")

# Legacy aliases (kept for backward compat; all point to fast model)
RESPONSE_FORMAT_MODEL = os.getenv("RESPONSE_FORMAT_MODEL", FAST_LLM_MODEL)
OFF_TOPIC_CLASSIFIER_MODEL = os.getenv("OFF_TOPIC_CLASSIFIER_MODEL", FAST_LLM_MODEL)
DB_ROUTER_MODEL = os.getenv("DB_ROUTER_MODEL", FAST_LLM_MODEL)

ENABLE_QUERY_LEARNING = os.getenv("ENABLE_QUERY_LEARNING", "true").lower() in ("true", "1", "yes")

# ── User-facing messages ─────────────────────────────────────────────────────
RETRIEVAL_FAILURE_MESSAGE = "I wasn't able to retrieve that information right now. Please try rephrasing your question."

DATABASE_CONNECTION_MESSAGE = (
    "I'm having trouble connecting to your database right now. "
    "Please contact Botivate support if this persists."
)

OFF_TOPIC_MESSAGE = (
    "I can only help with your business data. Try questions like:\n\n"
    "• How many pending tasks?\n"
    "• Show tasks assigned to [name]\n"
    "• What is [person]'s email?\n"
    "• Count of records by department"
)

# ── Shared client singletons ─────────────────────────────────────────────────
_openai_client: AsyncOpenAI | None = None  # type: ignore[assignment]
_fast_llm_client: AsyncOpenAI | None = None  # type: ignore[assignment]
