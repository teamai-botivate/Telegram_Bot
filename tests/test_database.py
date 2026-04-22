import socket
from unittest.mock import AsyncMock

import pytest

from app import database


def test_describe_connection_exception_timeout() -> None:
    msg = database._describe_connection_exception(TimeoutError())
    assert "timed out" in msg.lower()


def test_describe_connection_exception_hostname_resolution() -> None:
    msg = database._describe_connection_exception(socket.gaierror(8, "nodename nor servname provided"))
    assert "hostname could not be resolved" in msg.lower()


def test_describe_connection_exception_empty_message_falls_back_to_type_name() -> None:
    msg = database._describe_connection_exception(RuntimeError())
    assert "RuntimeError" in msg


@pytest.mark.asyncio
async def test_fetch_postgres_schema_timeout_has_actionable_error(monkeypatch) -> None:
    monkeypatch.setattr(database.asyncpg, "connect", AsyncMock(side_effect=TimeoutError()))

    with pytest.raises(ValueError) as exc_info:
        await database.fetch_postgres_schema("postgresql://user:pass@db.example.com:5432/postgres")

    assert "Failed to extract database blueprint" in str(exc_info.value)
    assert "timed out" in str(exc_info.value).lower()


@pytest.mark.asyncio
async def test_fetch_postgres_schema_requires_database_name() -> None:
    with pytest.raises(ValueError) as exc_info:
        await database.fetch_postgres_schema("postgresql://user:pass@db.example.com")

    assert "missing a database name" in str(exc_info.value)
