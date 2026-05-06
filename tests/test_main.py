import pytest

from app import main


@pytest.mark.asyncio
async def test_root_returns_ok() -> None:
    assert await main.root() == {"status": "ok", "service": "botivate-bot"}


@pytest.mark.asyncio
async def test_root_head_returns_ok() -> None:
    response = await main.root_head()

    assert response.status_code == 200
