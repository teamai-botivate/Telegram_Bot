from __future__ import annotations

import logging
import os
from collections import OrderedDict
from typing import Any

import httpx
from dotenv import load_dotenv

try:
    from openai import AsyncOpenAI, APIError, RateLimitError
except ImportError as _openai_error:  # pragma: no cover - exercised in environments without openai installed
    AsyncOpenAI = Any  # type: ignore[assignment]
    APIError = Exception  # type: ignore[assignment,misc]
    RateLimitError = Exception  # type: ignore[assignment,misc]
else:
    _openai_error = None

load_dotenv()

logger = logging.getLogger(__name__)

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "text-embedding-3-small")
EMBEDDING_DIM = 1536

_CACHE_MAX_ENTRIES = 500
_CACHE_MAX_TEXT_LEN = 500
_embedding_cache: "OrderedDict[str, list[float]]" = OrderedDict()

_openai_client: AsyncOpenAI | None = None


def _get_openai_client() -> AsyncOpenAI | None:
    global _openai_client
    if _openai_error is not None:
        logger.warning("openai package is not installed; embeddings unavailable.")
        return None
    if not OPENAI_API_KEY:
        logger.warning("OPENAI_API_KEY is not configured; embeddings unavailable.")
        return None
    if _openai_client is None:
        _openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY)
    return _openai_client


def _cache_get(text: str) -> list[float] | None:
    if len(text) > _CACHE_MAX_TEXT_LEN:
        return None
    value = _embedding_cache.get(text)
    if value is not None:
        _embedding_cache.move_to_end(text)
    return value


def _cache_set(text: str, vector: list[float]) -> None:
    if len(text) > _CACHE_MAX_TEXT_LEN:
        return
    _embedding_cache[text] = vector
    _embedding_cache.move_to_end(text)
    while len(_embedding_cache) > _CACHE_MAX_ENTRIES:
        _embedding_cache.popitem(last=False)


async def embed_text(text: str) -> list[float] | None:
    """Embed a single string. Returns None on missing key, missing package, or API failure."""
    if not text:
        return None

    cached = _cache_get(text)
    if cached is not None:
        return cached

    client = _get_openai_client()
    if client is None:
        return None

    try:
        response = await client.embeddings.create(model=EMBEDDING_MODEL, input=text)
    except RateLimitError as exc:
        logger.warning("OpenAI rate limit hit while embedding text: %s", exc)
        return None
    except APIError as exc:
        logger.warning("OpenAI API error while embedding text: %s", exc)
        return None
    except httpx.TimeoutException as exc:
        logger.warning("Timeout while embedding text: %s", exc)
        return None

    vector = list(response.data[0].embedding)
    _cache_set(text, vector)
    return vector


async def embed_batch(texts: list[str]) -> list[list[float] | None]:
    """Embed a list of strings in a single API call. Returns a parallel list; entries are None on failure."""
    if not texts:
        return []

    results: list[list[float] | None] = [None] * len(texts)
    pending_indices: list[int] = []
    pending_texts: list[str] = []

    for idx, text in enumerate(texts):
        if not text:
            continue
        cached = _cache_get(text)
        if cached is not None:
            results[idx] = cached
            continue
        pending_indices.append(idx)
        pending_texts.append(text)

    if not pending_texts:
        return results

    client = _get_openai_client()
    if client is None:
        return results

    try:
        response = await client.embeddings.create(model=EMBEDDING_MODEL, input=pending_texts)
    except RateLimitError as exc:
        logger.warning("OpenAI rate limit hit while embedding batch of %d: %s", len(pending_texts), exc)
        return results
    except APIError as exc:
        logger.warning("OpenAI API error while embedding batch of %d: %s", len(pending_texts), exc)
        return results
    except httpx.TimeoutException as exc:
        logger.warning("Timeout while embedding batch of %d: %s", len(pending_texts), exc)
        return results

    for item in response.data:
        idx = pending_indices[item.index]
        vector = list(item.embedding)
        results[idx] = vector
        _cache_set(pending_texts[item.index], vector)

    return results
