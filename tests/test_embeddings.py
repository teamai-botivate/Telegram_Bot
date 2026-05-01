from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from app import embeddings


@pytest.fixture(autouse=True)
def _reset_module_state(monkeypatch):
    embeddings._embedding_cache.clear()
    monkeypatch.setattr(embeddings, "_openai_client", None)
    monkeypatch.setattr(embeddings, "OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(embeddings, "_openai_error", None, raising=False)
    yield
    embeddings._embedding_cache.clear()


def _mock_client_with_response(monkeypatch, response):
    client = SimpleNamespace(embeddings=SimpleNamespace(create=AsyncMock(return_value=response)))
    monkeypatch.setattr(embeddings, "_get_openai_client", lambda: client)
    return client


async def test_embed_text_returns_vector(monkeypatch) -> None:
    response = SimpleNamespace(data=[SimpleNamespace(embedding=[0.1] * 1536)])
    client = _mock_client_with_response(monkeypatch, response)

    result = await embeddings.embed_text("how many orders today?")

    assert result is not None
    assert len(result) == 1536
    client.embeddings.create.assert_awaited_once()


async def test_embed_text_uses_cache_on_repeat(monkeypatch) -> None:
    response = SimpleNamespace(data=[SimpleNamespace(embedding=[0.2] * 1536)])
    client = _mock_client_with_response(monkeypatch, response)

    first = await embeddings.embed_text("repeat me")
    second = await embeddings.embed_text("repeat me")

    assert first == second
    client.embeddings.create.assert_awaited_once()


async def test_embed_text_skips_cache_for_long_strings(monkeypatch) -> None:
    response = SimpleNamespace(data=[SimpleNamespace(embedding=[0.3] * 1536)])
    client = _mock_client_with_response(monkeypatch, response)

    long_text = "x" * 600
    await embeddings.embed_text(long_text)
    await embeddings.embed_text(long_text)

    assert client.embeddings.create.await_count == 2
    assert long_text not in embeddings._embedding_cache


async def test_embed_text_returns_none_when_key_missing(monkeypatch) -> None:
    monkeypatch.setattr(embeddings, "OPENAI_API_KEY", "")
    monkeypatch.setattr(embeddings, "_openai_client", None)

    result = await embeddings.embed_text("anything")

    assert result is None


async def test_embed_text_returns_none_on_rate_limit(monkeypatch) -> None:
    create_mock = AsyncMock(side_effect=embeddings.RateLimitError("rate limited"))
    client = SimpleNamespace(embeddings=SimpleNamespace(create=create_mock))
    monkeypatch.setattr(embeddings, "_get_openai_client", lambda: client)

    result = await embeddings.embed_text("hello")

    assert result is None


async def test_embed_text_returns_none_on_timeout(monkeypatch) -> None:
    create_mock = AsyncMock(side_effect=httpx.TimeoutException("slow"))
    client = SimpleNamespace(embeddings=SimpleNamespace(create=create_mock))
    monkeypatch.setattr(embeddings, "_get_openai_client", lambda: client)

    result = await embeddings.embed_text("hello")

    assert result is None


async def test_embed_batch_single_api_call(monkeypatch) -> None:
    response = SimpleNamespace(data=[
        SimpleNamespace(index=0, embedding=[0.1] * 1536),
        SimpleNamespace(index=1, embedding=[0.2] * 1536),
        SimpleNamespace(index=2, embedding=[0.3] * 1536),
    ])
    client = _mock_client_with_response(monkeypatch, response)

    results = await embeddings.embed_batch(["a", "b", "c"])

    assert len(results) == 3
    assert all(r is not None and len(r) == 1536 for r in results)
    client.embeddings.create.assert_awaited_once()
    call_kwargs = client.embeddings.create.await_args.kwargs
    assert call_kwargs["input"] == ["a", "b", "c"]


async def test_embed_batch_uses_cache_for_known_entries(monkeypatch) -> None:
    embeddings._cache_set("cached-q", [0.9] * 1536)

    response = SimpleNamespace(data=[
        SimpleNamespace(index=0, embedding=[0.1] * 1536),
    ])
    client = _mock_client_with_response(monkeypatch, response)

    results = await embeddings.embed_batch(["cached-q", "fresh-q"])

    assert results[0] == [0.9] * 1536
    assert results[1] is not None and results[1][0] == 0.1
    call_kwargs = client.embeddings.create.await_args.kwargs
    assert call_kwargs["input"] == ["fresh-q"]


async def test_embed_batch_returns_nones_on_api_error(monkeypatch) -> None:
    class _StubAPIError(Exception):
        pass

    monkeypatch.setattr(embeddings, "APIError", _StubAPIError)
    create_mock = AsyncMock(side_effect=_StubAPIError("boom"))
    client = SimpleNamespace(embeddings=SimpleNamespace(create=create_mock))
    monkeypatch.setattr(embeddings, "_get_openai_client", lambda: client)

    results = await embeddings.embed_batch(["a", "b"])

    assert results == [None, None]


async def test_cache_evicts_oldest(monkeypatch) -> None:
    monkeypatch.setattr(embeddings, "_CACHE_MAX_ENTRIES", 3)

    embeddings._cache_set("a", [0.1] * 1536)
    embeddings._cache_set("b", [0.2] * 1536)
    embeddings._cache_set("c", [0.3] * 1536)
    embeddings._cache_set("d", [0.4] * 1536)

    assert "a" not in embeddings._embedding_cache
    assert list(embeddings._embedding_cache.keys()) == ["b", "c", "d"]


async def test_module_imports_without_key(monkeypatch) -> None:
    monkeypatch.setattr(embeddings, "OPENAI_API_KEY", "")
    monkeypatch.setattr(embeddings, "_openai_client", None)

    assert await embeddings.embed_text("anything") is None
    assert await embeddings.embed_batch(["a", "b"]) == [None, None]
