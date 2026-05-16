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

DATABASE_URL = os.getenv("DATABASE_URL", "")

FERNET_SECRET_KEY = os.getenv("FERNET_SECRET_KEY", "")

TENANT_DB_CONNECT_TIMEOUT_SECONDS = float(os.getenv("TENANT_DB_CONNECT_TIMEOUT_SECONDS", "30"))

TENANT_DB_CONNECT_RETRIES = int(os.getenv("TENANT_DB_CONNECT_RETRIES", "2"))

logger = logging.getLogger(__name__)

_tenant_pools: dict[str, asyncpg.Pool] = {}

_pool_lock = asyncio.Lock()

RUNTIME_SCHEMA_CACHE_TTL_SECONDS = float(os.getenv("RUNTIME_SCHEMA_CACHE_TTL_SECONDS", "300"))  # 5 minutes

_runtime_schema_cache: dict[str, tuple[float, str, str]] = {}  # {credential_id: (timestamp, schema, hints)}

SHEETS_CACHE_TTL_SECONDS = float(os.getenv("SHEETS_CACHE_TTL_SECONDS", "60"))  # 1 minute

_sheets_data_cache: dict[str, tuple[float, list[dict[str, Any]], str, str]] = {}  # {sheet_id: (timestamp, profiles, hints, title)}

class TenantDBConnectionError(Exception):
	"""Raised when a tenant database connection cannot be established."""

class QueryExecutionError(Exception):
	"""Raised when query execution against tenant DB fails."""

class SecurityError(Exception):
	"""Raised when a query violates security rules."""

_fernet: Fernet | None = None

def _convert_to_sqlalchemy_asyncpg_url(database_url: str) -> str:
	parsed = make_url(database_url)
	drivername = parsed.drivername

	if drivername in {"postgres", "postgresql"} or drivername.startswith("postgresql+"):
		return parsed.set(drivername="postgresql+asyncpg").render_as_string(hide_password=False)

	raise ValueError("DATABASE_URL must use a PostgreSQL scheme.")

META_SQLALCHEMY_DATABASE_URL = _convert_to_sqlalchemy_asyncpg_url(DATABASE_URL) if DATABASE_URL else ""

meta_engine: AsyncEngine | None = (
	create_async_engine(META_SQLALCHEMY_DATABASE_URL, echo=False, pool_pre_ping=True) if META_SQLALCHEMY_DATABASE_URL else None
)

session_factory: async_sessionmaker[AsyncSession] | None = (
	async_sessionmaker(meta_engine, expire_on_commit=False) if meta_engine else None
)

