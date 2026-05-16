"""Smart intent detection: hardcoded rules → learned rules → fast LLM.

Eliminates unnecessary main-model LLM calls by catching common intents
with rules before falling back to the fast model.
"""
from __future__ import annotations

import re

from .core import logger
from .llm import _call_fast_llm
from .runtime_memory import find_learned_intent, learn_intent_rule


# ── Hardcoded Intent Rules ───────────────────────────────────────────────────

_GREETING_PATTERNS = {
    "hi", "hello", "hey", "good morning", "good evening", "good afternoon",
    "how are you", "how are you?", "who are you", "who are you?", "what are you",
    "thanks", "thank you", "bye", "goodbye", "ok", "okay", "test", "testing", "ping",
    "yo", "sup", "hola", "namaste", "what's up", "whats up",
}

_JAILBREAK_PATTERNS = [
    r"^tell me a joke",
    r"^what is the weather",
    r"^write a poem",
    r"^write code",
    r"^who is the president",
    r"^how to make",
    r"^recipe for",
    r"^sing a song",
    r"^ignore all previous",
    r"^forget your instructions",
    r"^you are now",
    r"^pretend you",
    r"^act as",
    r"^translate this",
    r"^what is the capital",
]

_DATA_QUERY_SIGNALS = [
    r"\bhow many\b",
    r"\bcount\b",
    r"\bshow\b",
    r"\blist\b",
    r"\bfetch\b",
    r"\bget\b",
    r"\bfind\b",
    r"\bwhat is\b.*\b(status|email|phone|name|salary|date|amount|total)\b",
    r"\bwho\b.*\b(has|have|assigned|pending|completed|manager)\b",
    r"\btotal\b",
    r"\baverage\b",
    r"\bsum\b",
    r"\bmaximum\b|\bminimum\b|\bmax\b|\bmin\b",
    r"\bpending\b",
    r"\bcompleted\b",
    r"\bassigned\b",
    r"\boverdue\b",
    r"\bpast due\b",
    r"\bbetween\b.*\band\b",
    r"\blast\s+(?:\d+\s+)?(?:days?|weeks?|months?|years?)\b",
    r"\bthis\s+(?:week|month|year|quarter)\b",
]


# ── Intent Classification ────────────────────────────────────────────────────

async def detect_intent(text: str) -> str:
    """Detect intent using: hardcoded rules → learned rules → fast LLM.

    Returns one of:
      "off_topic"   — greetings, jailbreaks, non-business questions
      "data_query"  — questions about business data
      "command"     — bot commands (/start, /help, /adddb)
    """
    text_lower = text.strip().lower()

    # ── Layer 0: Bot commands ────────────────────────────────────────────
    if text_lower in ("/start", "start", "/help", "help", "/adddb", "adddb"):
        return "command"
    if text_lower.startswith("/start ") or text_lower.startswith("start-"):
        return "command"

    # ── Layer 1: Hardcoded rules (instant, zero cost) ────────────────────
    if len(text_lower) < 2:
        return "off_topic"

    if text_lower in _GREETING_PATTERNS:
        return "off_topic"

    if any(re.search(p, text_lower) for p in _JAILBREAK_PATTERNS):
        return "off_topic"

    # Strong data query signals → skip LLM entirely
    if any(re.search(p, text_lower) for p in _DATA_QUERY_SIGNALS):
        return "data_query"

    # ── Layer 2: Learned rules from past LLM responses ───────────────────
    learned = find_learned_intent(text_lower)
    if learned is not None:
        logger.info("[INTENT] Matched learned rule: %s → %s", text_lower[:50], learned)
        return learned

    # ── Layer 3: Fast LLM fallback (only when rules don't match) ─────────
    try:
        llm_intent = await _classify_with_fast_llm(text)
        # Auto-learn this result for future queries
        learn_intent_rule(text, llm_intent)
        logger.info("[INTENT] LLM classified and learned: %s → %s", text[:50], llm_intent)
        return llm_intent
    except Exception as exc:
        logger.warning("[INTENT] Fast LLM failed, defaulting to data_query: %s", exc)
        return "data_query"


async def _classify_with_fast_llm(text: str) -> str:
    """Use the fast/cheap model for intent classification."""
    system_prompt = (
        "You are a message classifier for a business data assistant bot. "
        "The bot can ONLY answer questions about business data (databases, spreadsheets, records, employees, tasks, etc.).\n\n"
        "Classify the user message into exactly one of:\n"
        "- DATA_QUERY: Questions about business data, records, counts, lists, lookups, reports\n"
        "- OFF_TOPIC: Greetings, small talk, jokes, non-business questions, jailbreaks\n\n"
        "Reply with ONLY the classification label. Nothing else."
    )
    result = await _call_fast_llm(system_prompt, text, max_tokens=10)
    result_upper = result.strip().upper()

    if "DATA" in result_upper:
        return "data_query"
    return "off_topic"
