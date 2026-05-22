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
        # Cerebras's current Llama 3.3 70B slug is not universally available.
        # gpt-oss-120b is on the Production tier with 65k context and the
        # fastest inference Cerebras offers.
        "base_url": "https://api.cerebras.ai/v1",
        "model": "gpt-oss-120b",
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

# Example-question generator runs on the fast LLM by default. For lowest latency
# you can set EXAMPLES_LLM_PROVIDER=cerebras + EXAMPLES_LLM_API_KEY=csk_... and
# EXAMPLES_LLM_MODEL=llama-3.3-70b — Cerebras returns the response in ~300ms,
# which makes the /start welcome message feel instant.
EXAMPLES_LLM_PROVIDER = os.getenv("EXAMPLES_LLM_PROVIDER", "").strip().lower() or None
EXAMPLES_LLM_API_KEY = os.getenv("EXAMPLES_LLM_API_KEY", "").strip() or None
EXAMPLES_LLM_MODEL = os.getenv("EXAMPLES_LLM_MODEL", "").strip() or None
if EXAMPLES_LLM_PROVIDER and not EXAMPLES_LLM_MODEL:
    EXAMPLES_LLM_MODEL = _PROVIDER_DEFAULTS.get(EXAMPLES_LLM_PROVIDER, {}).get("model")
EXAMPLES_LLM_BASE_URL = (
    _PROVIDER_DEFAULTS.get(EXAMPLES_LLM_PROVIDER, {}).get("base_url", "")
    if EXAMPLES_LLM_PROVIDER else ""
)

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

GREETING_REPLY = (
    "Hi there! 👋 I'm Botivate Bot — your business data assistant.\n\n"
    "Ask me anything about your company data and I'll fetch the answer in seconds. "
    "Some examples:\n"
    "• How many pending tasks?\n"
    "• Show me sales for last month\n"
    "• Who is responsible for the open orders?\n\n"
    "What would you like to know?"
)

THANKS_REPLY = "You're welcome! Let me know if you'd like to look up anything else. 🙂"

BYE_REPLY = "Goodbye! 👋 I'll be here whenever you need to check on your data."

# Words that should be treated as friendly greetings rather than generic off-topic.
_GREETING_TRIGGERS = {
    "hi", "hello", "hey", "hii", "hiii", "hyy", "yo", "sup",
    "good morning", "good afternoon", "good evening", "good night",
    "namaste", "hola", "what's up", "whats up",
    "how are you", "how are you?", "how are u", "how r u",
    "who are you", "who are you?", "what are you",
    "ping", "test", "testing",
}

_THANKS_TRIGGERS = {"thanks", "thank you", "thx", "ty", "thank u", "thanku"}

_FAREWELL_TRIGGERS = {"bye", "goodbye", "see you", "see ya", "cya", "gn", "gnight"}


def is_greeting(text: str) -> bool:
    """Return True if the text is a plain greeting (hi/hello/good morning/etc).

    Used to give tenants a personalised welcome (with schema-aware example
    questions) instead of the generic GREETING_REPLY.
    """
    cleaned = (text or "").strip().lower().rstrip("!.?")
    return cleaned in _GREETING_TRIGGERS


def pick_off_topic_reply(text: str) -> str:
    """Return a friendlier reply when the off-topic message is clearly a
    greeting, a thank-you, or a goodbye. Falls back to the generic
    capabilities message for anything else.
    """
    cleaned = (text or "").strip().lower().rstrip("!.?")
    if cleaned in _GREETING_TRIGGERS:
        return GREETING_REPLY
    if cleaned in _THANKS_TRIGGERS:
        return THANKS_REPLY
    if cleaned in _FAREWELL_TRIGGERS:
        return BYE_REPLY
    return OFF_TOPIC_MESSAGE

# ── Shared client singletons ─────────────────────────────────────────────────
_openai_client: AsyncOpenAI | None = None  # type: ignore[assignment]
_fast_llm_client: AsyncOpenAI | None = None  # type: ignore[assignment]
_examples_llm_client: AsyncOpenAI | None = None  # type: ignore[assignment]
