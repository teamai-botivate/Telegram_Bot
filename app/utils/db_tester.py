"""Shared connection-test helper for tenant Postgres credentials.

Used by:
- self-service onboarding submit endpoint (rejects bad creds before storing)
- admin endpoints that test a connection inline before persisting
- the schema-introspection path in app.database (calls open_postgres_connection)

Returns human-readable errors a non-technical user can act on.
"""

from __future__ import annotations

import socket
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import asyncpg


_ALLOWED_SSL_MODES = {"require", "prefer", "disable", "verify-ca", "verify-full"}


def _convert_to_asyncpg_url(database_url: str) -> str:
    """Strip SQLAlchemy driver prefixes (postgresql+asyncpg://) so asyncpg accepts the URL."""
    from sqlalchemy.engine import make_url

    parsed = make_url(database_url)
    drivername = parsed.drivername
    if drivername in {"postgres", "postgresql"} or drivername.startswith("postgresql+"):
        return parsed.set(drivername="postgresql").render_as_string(hide_password=False)
    raise ValueError("Connection URL must use a PostgreSQL scheme.")


def _normalize_postgres_url(connection_url: str, ssl_required: bool = True) -> tuple[str, str]:
    """Return (clean_url, ssl_arg) suitable for asyncpg.connect."""
    normalized_url = _convert_to_asyncpg_url(connection_url)
    parsed = urlparse(normalized_url)

    if not parsed.hostname:
        raise ValueError("Connection URL is missing a hostname.")
    if not parsed.path or parsed.path == "/":
        raise ValueError("Connection URL is missing a database name (for example: /postgres).")

    query_params = parse_qs(parsed.query)
    # asyncpg doesn't accept sslmode= in the URL — strip and pass via ssl= arg.
    ssl_mode = query_params.pop("sslmode", query_params.pop("ssl", [None]))[0]
    clean_query = urlencode({k: v[0] for k, v in query_params.items()})
    clean_url = urlunparse(parsed._replace(query=clean_query))

    if ssl_mode and ssl_mode in _ALLOWED_SSL_MODES:
        ssl_arg = ssl_mode
    elif ssl_required:
        ssl_arg = "require"
    else:
        ssl_arg = "prefer"

    return clean_url, ssl_arg


def _friendly_error(exc: Exception) -> str:
    """Translate asyncpg / network exceptions into a user-facing message."""
    message = str(exc).strip()
    lowered = message.lower()

    if isinstance(exc, asyncpg.InvalidPasswordError):
        return "Authentication failed. Check the username and password in your connection URL."
    if isinstance(exc, asyncpg.InvalidAuthorizationSpecificationError):
        return "Authentication failed. Check the username and password in your connection URL."
    if isinstance(exc, asyncpg.InvalidCatalogNameError):
        return "Database name not found. Check the database segment of your connection URL."
    if isinstance(exc, TimeoutError):
        return (
            "Connection timed out after 10 seconds. Check the host/port and confirm "
            "your database allows inbound traffic from Render."
        )
    if isinstance(exc, socket.gaierror):
        return "Could not resolve host. Check the database hostname in your connection URL."
    if isinstance(exc, ConnectionRefusedError):
        return "Connection refused. The database is reachable but rejecting connections — check the port and firewall rules."
    if isinstance(exc, OSError):
        return "Could not reach the database server. Check the host, port, and network access."

    if "ssl" in lowered and ("require" in lowered or "off" in lowered or "disabled" in lowered):
        return "SSL is required by this database. Append ?sslmode=require to your connection URL."
    if "password authentication failed" in lowered:
        return "Authentication failed. Check the username and password in your connection URL."
    if "does not exist" in lowered and "database" in lowered:
        return "Database name not found. Check the database segment of your connection URL."
    if "no pg_hba.conf entry" in lowered:
        return "The database is rejecting connections from this network. Check pg_hba/firewall rules."

    return message or f"{type(exc).__name__} (no error details provided)"


async def test_postgres_connection(
    connection_url: str,
    ssl_required: bool = True,
    timeout: float = 10.0,
) -> tuple[bool, str]:
    """Open a fresh asyncpg connection, run SELECT 1, close. Returns (ok, message)."""
    try:
        clean_url, ssl_arg = _normalize_postgres_url(connection_url, ssl_required=ssl_required)
    except ValueError as exc:
        return False, str(exc)

    conn: asyncpg.Connection | None = None
    try:
        conn = await asyncpg.connect(clean_url, ssl=ssl_arg, timeout=timeout)
        await conn.fetchval("SELECT 1")
        return True, ""
    except Exception as exc:
        return False, _friendly_error(exc)
    finally:
        if conn is not None:
            try:
                await conn.close()
            except Exception:
                pass


async def open_postgres_connection(
    connection_url: str,
    ssl_required: bool = True,
    timeout: float = 15.0,
) -> asyncpg.Connection:
    """Open and return an asyncpg connection ready for queries. Raises ValueError on
    URL problems; raises the original asyncpg/network exception on connect failure."""
    clean_url, ssl_arg = _normalize_postgres_url(connection_url, ssl_required=ssl_required)
    return await asyncpg.connect(clean_url, ssl=ssl_arg, timeout=timeout)
