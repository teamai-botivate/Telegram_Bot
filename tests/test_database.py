import socket
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app import database
from app.db import connection as db_connection
from app.db import postgres as db_postgres


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
    monkeypatch.setattr(db_connection.asyncpg, "connect", AsyncMock(side_effect=TimeoutError()))

    with pytest.raises(ValueError) as exc_info:
        await database.fetch_postgres_schema("postgresql://user:pass@db.example.com:5432/postgres")

    assert "Failed to extract database blueprint" in str(exc_info.value)
    assert "timed out" in str(exc_info.value).lower()


@pytest.mark.asyncio
async def test_fetch_postgres_schema_requires_database_name() -> None:
    with pytest.raises(ValueError) as exc_info:
        await database.fetch_postgres_schema("postgresql://user:pass@db.example.com")

    assert "missing a database name" in str(exc_info.value)


@pytest.mark.asyncio
async def test_open_fresh_connection_timeout_has_actionable_error(monkeypatch) -> None:
    credential = SimpleNamespace(
        db_type="postgresql",
        connection_url="encrypted",
        ssl_required=True,
        id="cred-id",
    )
    monkeypatch.setattr(db_connection, "get_tenant_credentials", AsyncMock(return_value=credential))
    monkeypatch.setattr(db_connection, "_decrypt_credential_value", lambda _: "postgresql://u:p@db.example.com:5432/postgres")
    monkeypatch.setattr(db_connection, "TENANT_DB_CONNECT_RETRIES", 0)
    monkeypatch.setattr(db_postgres.asyncpg, "connect", AsyncMock(side_effect=TimeoutError()))

    with pytest.raises(database.TenantDBConnectionError) as exc_info:
        await database._open_fresh_connection("tenant-id")

    assert "timed out" in str(exc_info.value).lower()


def test_sanitize_select_sql_rejects_non_select() -> None:
    with pytest.raises(database.SecurityError):
        database._sanitize_select_sql("DELETE FROM users")


def test_google_sheet_targeted_matches_count_rows_across_any_sheet_schema() -> None:
    profiles = [
        {
            "title": "Checklist",
            "rows": [
                {
                    "row_number": 19,
                    "values": {
                        "Given By": "Kavit Passary",
                        "Doer Name": "Anita Rathaur",
                        "Task Description": "Audit Closure Checklist",
                    },
                },
                {
                    "row_number": 20,
                    "values": {
                        "Given By": "Kavit Passary",
                        "Doer Name": "Ahitesh Tandan",
                        "Task Description": "Payroll Formula Setup",
                    },
                },
            ],
        },
        {
            "title": "Delegation",
            "rows": [
                {
                    "row_number": 2,
                    "values": {
                        "Assigned By": "Other Person",
                        "Owner": "Anita Rathaur",
                    },
                }
            ],
        },
    ]

    context = database._build_google_sheet_targeted_match_context(
        profiles,
        "How many tasks are assigned by Kavit Passary to Anita Rathaur?",
    )

    assert "Matched cell values from question: ['Kavit Passary', 'Anita Rathaur']" in context
    assert "Sheet `Checklist`: 1 rows contain all matched cell values." in context
    assert "Row 19" in context
    assert "Sheet `Delegation`: 0 rows contain all matched cell values." in context


@pytest.mark.asyncio
async def test_execute_tenant_query_executes_query_via_pool(monkeypatch) -> None:
    connection = SimpleNamespace(
        fetch=AsyncMock(return_value=[{"id": 1, "name": "Alice"}]),
    )

    class FakePool:
        def acquire(self):
            return self
        async def __aenter__(self):
            return connection
        async def __aexit__(self, *args):
            pass

    monkeypatch.setattr(db_postgres, "_get_pool_for_tenant", AsyncMock(return_value=FakePool()))

    rows = await database.execute_tenant_query("tenant-id", "SELECT id, name FROM users LIMIT 10")

    assert rows == [{"id": 1, "name": "Alice"}]
    assert connection.fetch.await_count == 1
    first_call_sql = connection.fetch.await_args_list[0].args[0]
    assert first_call_sql == "SELECT id, name FROM users LIMIT 10"


@pytest.mark.asyncio
async def test_execute_tenant_query_raises_query_error_when_query_fails(monkeypatch) -> None:
    class FakePgError(database.asyncpg.PostgresError):
        pass

    connection = SimpleNamespace(
        fetch=AsyncMock(side_effect=FakePgError("syntax error at or near FROM")),
    )

    class FakePool:
        def acquire(self):
            return self
        async def __aenter__(self):
            return connection
        async def __aexit__(self, *args):
            pass

    monkeypatch.setattr(db_postgres, "_get_pool_for_tenant", AsyncMock(return_value=FakePool()))

    with pytest.raises(database.QueryExecutionError) as exc_info:
        await database.execute_tenant_query("tenant-id", "SELECT id name FROM users")

    assert "Failed to execute query" in str(exc_info.value)
