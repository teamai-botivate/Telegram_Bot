"""Persistent runtime memory for learned intent rules and summary patterns.

Stores data in a JSON file that survives restarts, enabling the bot to
learn from LLM responses and avoid redundant calls over time.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ── Storage path ─────────────────────────────────────────────────────────────
_MEMORY_DIR = Path(__file__).parent
_MEMORY_FILE = _MEMORY_DIR / "runtime_memory.json"

# ── In-memory state ──────────────────────────────────────────────────────────
_lock = threading.RLock()

_intent_rules: list[dict[str, Any]] = []
# Each rule: {"pattern": str, "intent": str, "hits": int, "created_at": float}

_summary_patterns: list[dict[str, Any]] = []
# Each pattern: {"shape": str, "template_type": str, "hits": int, "created_at": float}

_MAX_INTENT_RULES = 500
_MAX_SUMMARY_PATTERNS = 200
_SAVE_DEBOUNCE_SECONDS = 5.0
_last_save_time: float = 0.0


# ── Load / Save ──────────────────────────────────────────────────────────────

def load_memory() -> None:
    """Load learned rules from disk. Called once at startup."""
    global _intent_rules, _summary_patterns
    if not _MEMORY_FILE.exists():
        logger.info("[MEMORY] No runtime_memory.json found — starting fresh.")
        return

    try:
        with open(_MEMORY_FILE, "r") as f:
            data = json.load(f)
        _intent_rules = data.get("intent_rules", [])
        _summary_patterns = data.get("summary_patterns", [])
        logger.info(
            "[MEMORY] Loaded %d intent rules, %d summary patterns.",
            len(_intent_rules), len(_summary_patterns),
        )
    except Exception as exc:
        logger.warning("[MEMORY] Failed to load runtime_memory.json: %s", exc)


def save_memory() -> None:
    """Persist learned rules to disk. Debounced to avoid excessive writes."""
    global _last_save_time
    now = time.monotonic()
    if now - _last_save_time < _SAVE_DEBOUNCE_SECONDS:
        return

    with _lock:
        _last_save_time = now
        try:
            data = {
                "intent_rules": _intent_rules[-_MAX_INTENT_RULES:],
                "summary_patterns": _summary_patterns[-_MAX_SUMMARY_PATTERNS:],
            }
            with open(_MEMORY_FILE, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as exc:
            logger.warning("[MEMORY] Failed to save runtime_memory.json: %s", exc)


# ── Intent Rules ─────────────────────────────────────────────────────────────

def find_learned_intent(question: str) -> str | None:
    """Check if a question matches a previously learned intent rule.

    Returns the intent string ("off_topic", "data_query", etc.) or None.
    Uses case-insensitive substring matching against stored patterns.
    """
    question_lower = question.strip().lower()
    with _lock:
        for rule in _intent_rules:
            pattern = rule.get("pattern", "").lower()
            if not pattern:
                continue
            # Exact match or close prefix match
            if question_lower == pattern or (
                len(pattern) > 5 and pattern in question_lower
            ):
                rule["hits"] = rule.get("hits", 0) + 1
                return rule["intent"]
    return None


def learn_intent_rule(question: str, intent: str) -> None:
    """Save a new intent rule learned from an LLM response."""
    question_lower = question.strip().lower()
    if len(question_lower) < 3:
        return

    with _lock:
        # Don't duplicate
        for rule in _intent_rules:
            if rule.get("pattern", "").lower() == question_lower:
                return

        _intent_rules.append({
            "pattern": question_lower,
            "intent": intent,
            "hits": 0,
            "created_at": time.time(),
        })

        # Trim oldest rules
        while len(_intent_rules) > _MAX_INTENT_RULES:
            _intent_rules.pop(0)

    save_memory()


# ── Summary Patterns ─────────────────────────────────────────────────────────

def classify_result_shape(rows: list[dict[str, Any]]) -> str:
    """Classify the shape of SQL results for template selection.

    Returns one of:
      "single_count"  — 1 row, 1 column, numeric value
      "single_row"    — 1 row, multiple columns
      "short_list"    — 2-5 rows
      "medium_list"   — 6-20 rows
      "large_list"    — 21+ rows
      "empty"         — 0 rows
    """
    if not rows:
        return "empty"

    row_count = len(rows)
    col_count = len(rows[0]) if rows else 0

    if row_count == 1 and col_count == 1:
        # Check if the single value is numeric
        val = list(rows[0].values())[0]
        if isinstance(val, (int, float)):
            return "single_count"
        try:
            float(str(val))
            return "single_count"
        except (ValueError, TypeError):
            pass
        return "single_row"

    if row_count == 1:
        return "single_row"
    if row_count <= 5:
        return "short_list"
    if row_count <= 20:
        return "medium_list"
    return "large_list"


def record_summary_pattern(shape: str, template_type: str) -> None:
    """Record which template/strategy worked for a given result shape."""
    with _lock:
        # Update existing or add new
        for pattern in _summary_patterns:
            if pattern.get("shape") == shape and pattern.get("template_type") == template_type:
                pattern["hits"] = pattern.get("hits", 0) + 1
                break
        else:
            _summary_patterns.append({
                "shape": shape,
                "template_type": template_type,
                "hits": 1,
                "created_at": time.time(),
            })

            while len(_summary_patterns) > _MAX_SUMMARY_PATTERNS:
                _summary_patterns.pop(0)

    save_memory()


def should_use_template(shape: str) -> bool:
    """Determine if a result shape should use a template response (no LLM).

    Templates are used for simple shapes that have been successfully
    handled by templates before, or for trivially simple shapes.
    """
    # Always use templates for trivial shapes
    # single_row is intentionally excluded: it often contains multi-column structured
    # data (e.g. table metadata) that needs LLM narrative rather than a raw bullet dump.
    if shape in ("single_count", "empty", "short_list"):
        return True

    # Check if templates have historically worked for this shape
    with _lock:
        template_hits = 0
        llm_hits = 0
        for pattern in _summary_patterns:
            if pattern.get("shape") != shape:
                continue
            if pattern.get("template_type") == "template":
                template_hits += pattern.get("hits", 0)
            else:
                llm_hits += pattern.get("hits", 0)

        # Use template if it's been successful more than 60% of the time
        total = template_hits + llm_hits
        if total >= 3 and template_hits / total >= 0.6:
            return True

    return False
