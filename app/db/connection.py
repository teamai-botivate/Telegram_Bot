from __future__ import annotations
import asyncio
import json
import logging
import os
import re
import socket
import time
import uuid
from datetime import datetime, timezone
from typing import Any

import asyncpg
from cryptography.fernet import Fernet, InvalidToken
from dotenv import load_dotenv
from sqlalchemy import or_, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.models import Base, RegisteredClient, Tenant, TenantDBCredential

from .core import *
from .security import _decrypt_credential_value
from .crud import get_tenant_credentials, _touch_last_connected
def _convert_to_asyncpg_url(database_url: str) -> str:
	parsed = make_url(database_url)
	drivername = parsed.drivername

	if drivername in {"postgres", "postgresql"}:
		return parsed.set(drivername="postgresql").render_as_string(hide_password=False)

	if drivername.startswith("postgresql+"):
		return parsed.set(drivername="postgresql").render_as_string(hide_password=False)

	raise ValueError("DATABASE_URL must use a PostgreSQL scheme.")

def _describe_connection_exception(exc: Exception) -> str:
	message = str(exc).strip()
	if isinstance(exc, TimeoutError):
		return "Connection timed out. Verify host/port, SSL settings, and that your DB allows inbound traffic from Render."
	if isinstance(exc, socket.gaierror):
		return "Database hostname could not be resolved. Check the DB host in your connection URL."
	if message:
		return message
	return f"{type(exc).__name__} (no error details provided)"

def _quote_ident(identifier: str) -> str:
	return '"' + identifier.replace('"', '""') + '"'

def _resolve_tenant_dsn(credential: TenantDBCredential) -> tuple[str, str]:
	"""Decrypt and normalize a tenant's connection URL. Returns (clean_url, ssl_arg)."""
	from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

	try:
		connection_string = _decrypt_credential_value(credential.connection_url)
	except (InvalidToken, ValueError):
		raise TenantDBConnectionError("Tenant database credentials are invalid or could not be decrypted.")

	normalized_url = _convert_to_asyncpg_url(connection_string)
	parsed = urlparse(normalized_url)
	if not parsed.hostname:
		raise TenantDBConnectionError("Tenant DB hostname is missing in connection URL.")
	if not parsed.path or parsed.path == "/":
		raise TenantDBConnectionError("Tenant DB name is missing in connection URL.")
	query_params = parse_qs(parsed.query)

	# asyncpg doesn't understand sslmode=; strip it and pass ssl= explicitly
	ssl_mode = query_params.pop("sslmode", query_params.pop("ssl", [None]))[0]
	clean_query = urlencode({k: v[0] for k, v in query_params.items()})
	clean_url = urlunparse(parsed._replace(query=clean_query))

	# Determine SSL setting
	if ssl_mode and ssl_mode in ("require", "prefer", "disable", "verify-ca", "verify-full"):
		ssl_arg = ssl_mode
	elif credential.ssl_required:
		ssl_arg = "require"
	else:
		ssl_arg = "prefer"

	return clean_url, ssl_arg

async def _open_fresh_connection(tenant_id: uuid.UUID | str) -> asyncpg.Connection:
	"""Open a direct (non-pooled) connection. Used for schema introspection."""
	credential = await get_tenant_credentials(tenant_id)
	if credential is None:
		raise TenantDBConnectionError("Tenant database is not configured yet.")

	if credential.db_type.lower() != "postgresql":
		raise TenantDBConnectionError("Only PostgreSQL tenant databases are currently supported.")

	clean_url, ssl_arg = _resolve_tenant_dsn(credential)

	try:
		last_error: Exception | None = None
		attempts = max(1, TENANT_DB_CONNECT_RETRIES + 1)
		for attempt in range(1, attempts + 1):
			try:
				connection = await asyncpg.connect(
					clean_url,
					ssl=ssl_arg,
					timeout=TENANT_DB_CONNECT_TIMEOUT_SECONDS,
					statement_cache_size=0,
				)
				await _touch_last_connected(credential.id)
				return connection
			except Exception as exc:
				last_error = exc
				if attempt < attempts:
					await asyncio.sleep(0.3)

		detail = _describe_connection_exception(last_error) if last_error else "unknown error"
		raise TenantDBConnectionError(f"Could not connect to tenant database: {detail}")
	except TenantDBConnectionError:
		raise
	except Exception as exc:
		raise TenantDBConnectionError(f"Could not connect to tenant database: {_describe_connection_exception(exc)}")

async def get_tenant_pool(tenant_id: str, connection_string: str, ssl_arg: str) -> asyncpg.Pool:
	"""Return a cached connection pool for the given tenant, creating one if needed."""
	async with _pool_lock:
		if tenant_id not in _tenant_pools:
			_tenant_pools[tenant_id] = await asyncpg.create_pool(
				connection_string,
				ssl=ssl_arg,
				min_size=1,
				max_size=3,
				timeout=TENANT_DB_CONNECT_TIMEOUT_SECONDS,
				command_timeout=30,
				max_inactive_connection_lifetime=60,
				statement_cache_size=0,
			)
		return _tenant_pools[tenant_id]

async def _evict_tenant_pool(tenant_id: str) -> None:
	"""Force-close and remove a stale pool from the cache."""
	async with _pool_lock:
		pool = _tenant_pools.pop(tenant_id, None)
		if pool is not None:
			try:
				pool.terminate()
			except Exception:
				pass
	logger.info("Evicted stale pool for tenant %s", tenant_id)

async def _get_pool_for_tenant(tenant_id: uuid.UUID | str) -> asyncpg.Pool:
	"""Resolve credentials and return a pooled connection for a tenant."""
	credential = await get_tenant_credentials(tenant_id)
	if credential is None:
		raise TenantDBConnectionError("Tenant database is not configured yet.")
	return await _get_pool_for_credential(credential)

async def _get_pool_for_credential(credential: TenantDBCredential) -> asyncpg.Pool:
	"""Pool a connection keyed by the credential row id (so a tenant with multiple DBs
	gets one pool per DB)."""
	if credential.db_type.lower() != "postgresql":
		raise TenantDBConnectionError("Only PostgreSQL tenant databases are currently supported.")

	clean_url, ssl_arg = _resolve_tenant_dsn(credential)
	cache_key = str(credential.id)

	try:
		pool = await get_tenant_pool(cache_key, clean_url, ssl_arg)
		await _touch_last_connected(credential.id)
		return pool
	except TenantDBConnectionError:
		raise
	except Exception as exc:
		raise TenantDBConnectionError(f"Could not connect to tenant database: {_describe_connection_exception(exc)}")

