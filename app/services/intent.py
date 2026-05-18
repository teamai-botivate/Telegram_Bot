"""Smart intent detection: hardcoded off-topic rules → learned rules → default to data_query.

Philosophy: This is a business data bot. ASSUME the user wants data unless the
message is clearly off-topic (greeting, jailbreak, small talk). If an ambiguous
query slips through as data_query, the SQL pipeline will simply return no results
— which is a safe, graceful outcome. False negatives (blocking real queries) are
far worse than false positives (trying and failing).
"""
from __future__ import annotations

import re

from .core import logger
from .runtime_memory import find_learned_intent, learn_intent_rule


# ── Off-topic patterns (the ONLY hardcoded rules) ───────────────────────────
# These are things that are NEVER business data questions.

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


# ── Intent Classification ────────────────────────────────────────────────────

async def detect_intent(text: str) -> str:
    """Detect intent using: off-topic rules → learned rules → default data_query.

    Returns one of:
      "off_topic"   — greetings, jailbreaks, obvious non-business messages
      "data_query"  — anything else (default — let the SQL pipeline decide)
      "command"     — bot commands (/start, /help, /adddb)
    """
    text_lower = text.strip().lower()

    # ── Layer 0: Bot commands ────────────────────────────────────────────
    if text_lower in ("/start", "start", "/help", "help", "/adddb", "adddb"):
        return "command"
    if text_lower.startswith("/start ") or text_lower.startswith("start-"):
        return "command"

    # ── Layer 1: Obviously off-topic (instant, zero cost) ────────────────
    if len(text_lower) < 2:
        return "off_topic"

    if text_lower in _GREETING_PATTERNS:
        return "off_topic"

    if any(re.search(p, text_lower) for p in _JAILBREAK_PATTERNS):
        return "off_topic"

    # ── Layer 2: Learned rules from past LLM responses ───────────────────
    learned = find_learned_intent(text_lower)
    if learned is not None:
        logger.debug("[INTENT] Matched learned rule: %s → %s", text_lower[:50], learned)
        return learned

    # ── Layer 3: Default to data_query ───────────────────────────────────
    # In a business data bot, assume the user wants data. If it's truly
    # off-topic, the SQL pipeline will return no results gracefully.
    logger.debug("[INTENT] No off-topic match — defaulting to data_query: %s", text_lower[:50])
    return "data_query"
