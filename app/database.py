from __future__ import annotations

import asyncio
import logging
import os
import re
import socket
import uuid
from datetime import datetime, timezone
from typing import Any

import asyncpg
from cryptography.fernet import Fernet, InvalidToken
from dotenv import load_dotenv
from sqlalchemy import or_, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from .models import Base, Tenant, TenantDBCredential

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "")
FERNET_SECRET_KEY = os.getenv("FERNET_SECRET_KEY", "")
TENANT_DB_CONNECT_TIMEOUT_SECONDS = float(os.getenv("TENANT_DB_CONNECT_TIMEOUT_SECONDS", "30"))
TENANT_DB_CONNECT_RETRIES = int(os.getenv("TENANT_DB_CONNECT_RETRIES", "2"))
logger = logging.getLogger(__name__)

# ── Per-tenant connection pool cache ──
_tenant_pools: dict[str, asyncpg.Pool] = {}
_pool_lock = asyncio.Lock()


class TenantDBConnectionError(Exception):
	"""Raised when a tenant database connection cannot be established."""


class QueryExecutionError(Exception):
	"""Raised when query execution against tenant DB fails."""


class SecurityError(Exception):
	"""Raised when a query violates security rules."""


_fernet: Fernet | None = None


def _get_fernet() -> Fernet:
	global _fernet

	if not FERNET_SECRET_KEY:
		raise RuntimeError("FERNET_SECRET_KEY is not configured. Add it to your .env file.")

	if _fernet is None:
		_fernet = Fernet(FERNET_SECRET_KEY.encode())

	return _fernet


def encrypt_credential_value(value: str) -> str:
	return _get_fernet().encrypt(value.encode()).decode()


def _decrypt_credential_value(value: str) -> str:
	return _get_fernet().decrypt(value.encode()).decode()

def _convert_to_sqlalchemy_asyncpg_url(database_url: str) -> str:
	parsed = make_url(database_url)
	drivername = parsed.drivername

	if drivername in {"postgres", "postgresql"} or drivername.startswith("postgresql+"):
		return parsed.set(drivername="postgresql+asyncpg").render_as_string(hide_password=False)

	raise ValueError("DATABASE_URL must use a PostgreSQL scheme.")


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


def _sanitize_select_sql(sql: str, allow_select_star: bool = False) -> str:
	cleaned = sql.strip().rstrip(";").strip()
	if not cleaned:
		raise SecurityError("Query is empty.")

	if ";" in cleaned:
		raise SecurityError("Multiple statements are not allowed.")

	lowered = cleaned.lower()
	if not lowered.startswith("select"):
		raise SecurityError("Only SELECT statements are allowed.")

	blocked_keywords = ("insert", "update", "delete", "drop", "truncate", "alter", "create", "grant", "revoke")
	for keyword in blocked_keywords:
		if re.search(rf"\b{keyword}\b", lowered):
			raise SecurityError(f"Disallowed keyword detected: {keyword.upper()}")

	if not allow_select_star and re.search(r"\bselect\s+\*", lowered):
		raise SecurityError("SELECT * is not allowed for this execution path.")

	return cleaned

META_SQLALCHEMY_DATABASE_URL = _convert_to_sqlalchemy_asyncpg_url(DATABASE_URL) if DATABASE_URL else ""

meta_engine: AsyncEngine | None = (
	create_async_engine(META_SQLALCHEMY_DATABASE_URL, echo=False, pool_pre_ping=True) if META_SQLALCHEMY_DATABASE_URL else None
)
session_factory: async_sessionmaker[AsyncSession] | None = (
	async_sessionmaker(meta_engine, expire_on_commit=False) if meta_engine else None
)


async def create_tables() -> None:
	if meta_engine is None:
		raise RuntimeError("DATABASE_URL is not configured. Add it to your .env file.")

	async with meta_engine.begin() as connection:
		await connection.run_sync(Base.metadata.create_all)


async def get_tenant_by_chat_id(chat_id: str) -> Tenant | None:
	if session_factory is None:
		raise RuntimeError("DATABASE_URL is not configured. Add it to your .env file.")

	statement = select(Tenant).where(or_(Tenant.telegram_chat_id == chat_id, Tenant.whatsapp_number == chat_id))

	async with session_factory() as session:
		result = await session.execute(statement)
		return result.scalar_one_or_none()


async def get_tenant_credentials(tenant_id: uuid.UUID | str) -> TenantDBCredential | None:
	if session_factory is None:
		raise RuntimeError("DATABASE_URL is not configured. Add it to your .env file.")

	tenant_uuid = uuid.UUID(str(tenant_id))
	statement = select(TenantDBCredential).where(TenantDBCredential.tenant_id == tenant_uuid)

	async with session_factory() as session:
		result = await session.execute(statement)
		return result.scalar_one_or_none()



async def get_active_modules(tenant_id: uuid.UUID) -> list[str]:
	if session_factory is None:
		raise RuntimeError("DATABASE_URL is not configured. Add it to your .env file.")

	statement = select(Tenant.active_modules).where(Tenant.id == tenant_id)

	async with session_factory() as session:
		result = await session.execute(statement)
		active_modules = result.scalar_one_or_none()

	if not active_modules:
		return []

	return list(active_modules)


async def save_tenant_credentials(
	tenant_id: uuid.UUID | str,
	db_type: str,
	connection_url: str,
	schema_blueprint: str | None = None,
	auto_schema_hints: str | None = None,
	ssl_required: bool = True,
	google_credentials: str | None = None,
) -> TenantDBCredential:
	if session_factory is None:
		raise RuntimeError("DATABASE_URL is not configured. Add it to your .env file.")

	tenant_uuid = uuid.UUID(str(tenant_id))

	async with session_factory() as session:
		tenant = await session.get(Tenant, tenant_uuid)
		if tenant is None:
			raise ValueError("Tenant not found.")

		statement = select(TenantDBCredential).where(TenantDBCredential.tenant_id == tenant_uuid)
		existing_result = await session.execute(statement)
		credential = existing_result.scalar_one_or_none()

		encrypted_url = encrypt_credential_value(connection_url)
		encrypted_creds = encrypt_credential_value(google_credentials) if google_credentials else None

		if credential is None:
			credential = TenantDBCredential(
				tenant_id=tenant_uuid,
				db_type=db_type,
				connection_url=encrypted_url,
				schema_blueprint=schema_blueprint,
				auto_schema_hints=auto_schema_hints,
				ssl_required=ssl_required,
				google_credentials=encrypted_creds,
			)
			session.add(credential)
		else:
			credential.db_type = db_type
			credential.connection_url = encrypted_url
			if schema_blueprint is not None:
				credential.schema_blueprint = schema_blueprint
			if auto_schema_hints is not None:
				credential.auto_schema_hints = auto_schema_hints
			credential.ssl_required = ssl_required
			if encrypted_creds:
				credential.google_credentials = encrypted_creds

		await session.commit()
		await session.refresh(credential)

		return credential



async def _touch_last_connected(credential_id: uuid.UUID) -> None:
	if session_factory is None:
		return

	async with session_factory() as session:
		credential = await session.get(TenantDBCredential, credential_id)
		if credential is None:
			return

		credential.last_connected_at = datetime.now(timezone.utc)
		await session.commit()


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

	if credential.db_type.lower() != "postgresql":
		raise TenantDBConnectionError("Only PostgreSQL tenant databases are currently supported.")

	clean_url, ssl_arg = _resolve_tenant_dsn(credential)
	tid = str(tenant_id)

	try:
		pool = await get_tenant_pool(tid, clean_url, ssl_arg)
		await _touch_last_connected(credential.id)
		return pool
	except TenantDBConnectionError:
		raise
	except Exception as exc:
		raise TenantDBConnectionError(f"Could not connect to tenant database: {_describe_connection_exception(exc)}")


async def execute_tenant_query(
	tenant_id: uuid.UUID | str,
	sql: str,
	*params: Any,
	allow_select_star: bool = False,
) -> list[dict[str, Any]]:
	tid = str(tenant_id)

	for attempt in range(2):
		pool = await _get_pool_for_tenant(tenant_id)
		try:
			async with pool.acquire() as connection:
				safe_sql = _sanitize_select_sql(sql, allow_select_star=allow_select_star)
				logger.info(
					"Tenant query attempt tenant_id=%s timestamp=%s sql=%s",
					tenant_id,
					datetime.now(timezone.utc).isoformat(),
					safe_sql,
				)

				rows = await connection.fetch(safe_sql, *params)
				return [dict(row) for row in rows]
		except (TimeoutError, OSError, ConnectionResetError) as e:
			# Pool connection is stale — evict and retry once
			logger.warning("Pool connection failed for tenant %s (attempt %d): %s", tid, attempt + 1, e)
			await _evict_tenant_pool(tid)
			if attempt == 1:
				raise TenantDBConnectionError(f"Could not connect to tenant database after retry: {e}")
		except QueryExecutionError:
			raise
		except SecurityError:
			raise
		except asyncpg.PostgresError as e:
			logger.error(f"PostgresError executing tenant query. SQL: {sql} | Error: {e}")
			raise QueryExecutionError(f"Failed to execute query: {e}")
		except Exception as e:
			logger.error(f"Unexpected error executing tenant query: {e}")
			raise QueryExecutionError("An unexpected error occurred while running tenant query.")


async def explain_validate_sql(tenant_id: uuid.UUID | str, sql: str) -> tuple[bool, str]:
	"""Run EXPLAIN on the SQL against the tenant's database to validate structure.

	Returns (is_valid, error_message). If valid, error_message is empty.
	This catches wrong column names, wrong table names, bad joins, and
	syntax errors — without actually executing the query or touching data.
	Works with any tenant schema automatically.
	"""
	tid = str(tenant_id)
	try:
		pool = await _get_pool_for_tenant(tenant_id)
		async with pool.acquire() as connection:
			await connection.fetch(f"EXPLAIN {sql}")
		return True, ""
	except (TimeoutError, OSError, ConnectionResetError) as e:
		logger.warning("EXPLAIN connection failed for tenant %s: %s", tid, e)
		await _evict_tenant_pool(tid)
		# Connection issue, not SQL issue — let it pass through
		return True, ""
	except asyncpg.PostgresError as e:
		error_msg = str(e)
		logger.info("[EXPLAIN_FAIL] tenant=%s sql=%s error=%s", tid, sql, error_msg)
		return False, error_msg
	except Exception as e:
		logger.warning("EXPLAIN unexpected error for tenant %s: %s", tid, e)
		# Don't block on unexpected errors — let execution try
		return True, ""


async def create_tenant_record(company_name: str, active_modules: list[str]) -> uuid.UUID:
	if session_factory is None:
		raise RuntimeError("DATABASE_URL is not configured.")

	tenant = Tenant(company_name=company_name, active_modules=active_modules)
	async with session_factory() as session:
		session.add(tenant)
		await session.commit()
		await session.refresh(tenant)
		return tenant.id


async def update_tenant_chat_id(tenant_id: uuid.UUID | str, platform: str, chat_id: str) -> None:
	if session_factory is None:
		return

	tenant_uuid = uuid.UUID(str(tenant_id))
	async with session_factory() as session:
		tenant = await session.get(Tenant, tenant_uuid)
		if tenant is None:
			raise ValueError("Tenant not found.")

		if platform.lower() == "telegram":
			tenant.telegram_chat_id = chat_id
		elif platform.lower() == "whatsapp":
			tenant.whatsapp_number = chat_id

		await session.commit()


async def fetch_postgres_schema(connection_string: str) -> tuple[str, str]:
	connection: asyncpg.Connection | None = None
	try:
		# asyncpg doesn't understand sslmode= in URL; strip it and pass ssl= explicitly.
		from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

		normalized_url = _convert_to_asyncpg_url(connection_string)
		parsed = urlparse(normalized_url)
		if not parsed.hostname:
			raise ValueError("Connection URL is missing a hostname.")
		if not parsed.path or parsed.path == "/":
			raise ValueError("Connection URL is missing a database name (for example: /postgres).")
		query_params = parse_qs(parsed.query)

		ssl_mode = query_params.pop("sslmode", query_params.pop("ssl", ["require"]))[0]
		clean_query = urlencode({k: v[0] for k, v in query_params.items()})
		clean_url = urlunparse(parsed._replace(query=clean_query))

		allowed_ssl_modes = {"require", "prefer", "disable", "verify-ca", "verify-full"}
		ssl_arg = ssl_mode if ssl_mode in allowed_ssl_modes else "require"

		connection = await asyncpg.connect(clean_url, ssl=ssl_arg, timeout=15)

		enum_sql = """
		SELECT t.typname AS enum_name, array_agg(e.enumlabel::text) AS enum_values
		FROM pg_type t
		JOIN pg_enum e ON t.oid = e.enumtypid
		GROUP BY t.typname;
		"""
		enum_rows = await connection.fetch(enum_sql)
		enums = {row["enum_name"]: row["enum_values"] for row in enum_rows}

		column_sql = """
		SELECT table_schema, table_name, column_name, data_type, udt_name, is_nullable
		FROM information_schema.columns
		WHERE table_schema NOT IN ('information_schema', 'pg_catalog', 'auth', 'storage', 'vault', 'realtime', 'extensions')
		ORDER BY table_schema, table_name, ordinal_position;
		"""
		rows = await connection.fetch(column_sql)

		fk_sql = """
		SELECT
			tc.table_schema,
			tc.table_name,
			kcu.column_name,
			ccu.table_schema AS foreign_table_schema,
			ccu.table_name AS foreign_table_name,
			ccu.column_name AS foreign_column_name
		FROM information_schema.table_constraints tc
		JOIN information_schema.key_column_usage kcu
			ON tc.constraint_name = kcu.constraint_name
			AND tc.table_schema = kcu.table_schema
		JOIN information_schema.constraint_column_usage ccu
			ON ccu.constraint_name = tc.constraint_name
			AND ccu.table_schema = tc.table_schema
		WHERE tc.constraint_type = 'FOREIGN KEY'
			AND tc.table_schema NOT IN ('information_schema', 'pg_catalog', 'auth', 'storage', 'vault', 'realtime', 'extensions')
		ORDER BY tc.table_schema, tc.table_name, kcu.column_name;
		"""
		fk_rows = await connection.fetch(fk_sql)

		status_keywords = {
			"submitted",
			"completed",
			"done",
			"approved",
			"closed",
			"finished",
			"resolved",
			"verified",
			"paid",
			"delivered",
			"started",
			"ended",
			"cancelled",
		}
		completion_table_keywords = {"done", "completed", "archived", "history", "log", "audit"}
		reference_column_names = {"id", "user_id", "employee_id", "created_by", "assigned_to", "given_by"}
		date_like_types = {"date", "timestamp without time zone", "timestamp with time zone"}

		tables: dict[str, dict[str, Any]] = {}
		for row in rows:
			schema = row["table_schema"]
			table = row["table_name"]
			column = row["column_name"]
			data_type = row["data_type"]
			udt_name = row["udt_name"]
			try:
				is_nullable = row["is_nullable"]
			except Exception:
				is_nullable = "YES"
			nullable = str(is_nullable).upper() == "YES"

			if data_type == "USER-DEFINED" and udt_name in enums:
				data_type = f"enum({', '.join(enums[udt_name])})"

			full_name = f"{schema}.{table}" if schema != "public" else table
			if full_name not in tables:
				tables[full_name] = {
					"schema": schema,
					"table": table,
					"columns": [],
					"column_meta": [],
					"text_columns": [],
					"bool_columns": [],
					"fks": [],
				}
			tables[full_name]["columns"].append(f"{column} ({data_type})")
			tables[full_name]["column_meta"].append(
				{
					"name": column,
					"data_type": data_type,
					"nullable": nullable,
				}
			)
			if data_type.lower() in {"text", "character varying", "character", "varchar"}:
				tables[full_name]["text_columns"].append(column)
			if data_type.lower() == "boolean":
				tables[full_name]["bool_columns"].append(column)

		fk_details: list[dict[str, str]] = []
		for fk in fk_rows:
			src_schema = fk["table_schema"]
			src_table = fk["table_name"]
			src_col = fk["column_name"]
			dst_schema = fk["foreign_table_schema"]
			dst_table = fk["foreign_table_name"]
			dst_col = fk["foreign_column_name"]

			src_full = f"{src_schema}.{src_table}" if src_schema != "public" else src_table
			dst_full = f"{dst_schema}.{dst_table}" if dst_schema != "public" else dst_table
			if src_full in tables:
				tables[src_full]["fks"].append(f"FK: {src_full}.{src_col} -> {dst_full}.{dst_col}")
			fk_details.append(
				{
					"src_table": src_full,
					"src_col": src_col,
					"dst_table": dst_full,
					"dst_col": dst_col,
				}
			)

		relationship_lines: list[str] = []
		for fk in fk_details:
			relationship_lines.append(f"{fk['src_table']}.{fk['src_col']} -> {fk['dst_table']}.{fk['dst_col']}")

		fk_by_table_col: dict[tuple[str, str], dict[str, str]] = {}
		for fk in fk_details:
			fk_by_table_col[(fk["src_table"], fk["src_col"])] = fk

		auto_hints_lines: list[str] = []

		blueprint = "Database Blueprint (PostgreSQL):\n"
		blueprint += "RELATIONSHIPS (use these for JOINs):\n"
		if relationship_lines:
			for rel in relationship_lines:
				blueprint += rel + "\n"
		else:
			blueprint += "(none)\n"
		blueprint += "\nTABLES:\n"
		for table_name, info in tables.items():
			schema = info["schema"]
			table = info["table"]
			quoted_schema = _quote_ident(schema)
			quoted_table = _quote_ident(table)

			row_count_str = "unknown"
			try:
				count_row = await connection.fetchrow(f"SELECT COUNT(*)::bigint AS cnt FROM {quoted_schema}.{quoted_table}")
				if count_row is not None:
					row_count_str = str(count_row["cnt"])
			except Exception:
				pass

			blueprint += f"Table `{table_name}` | Rows: ~{row_count_str}\n"
			blueprint += f"Columns: {', '.join(info['columns'])}\n"

			table_hint_lines: list[str] = []

			# a) Nullable status timestamps/dates
			for col_meta in info.get("column_meta", []):
				col_name = col_meta["name"]
				dtype = str(col_meta["data_type"]).lower()
				nullable = bool(col_meta.get("nullable", True))
				if not nullable:
					continue
				if dtype not in date_like_types:
					continue
				lowered = col_name.lower()
				if any(k in lowered for k in status_keywords):
					hint = (
						f"Status hint: {table_name}.{col_name} IS NULL = pending/incomplete, "
						f"IS NOT NULL = done/complete"
					)
					table_hint_lines.append(hint)

			# b) Boolean columns
			for bool_col in info.get("bool_columns", []):
				table_hint_lines.append(f"Boolean: {bool_col} -- use = TRUE or = FALSE, never ILIKE")

			for text_col in info["text_columns"]:
				samples: list[str] = []
				try:
					quoted_col = _quote_ident(text_col)
					sample_rows = await connection.fetch(
						f"SELECT DISTINCT {quoted_col} AS value FROM {quoted_schema}.{quoted_table} "
						f"WHERE {quoted_col} IS NOT NULL LIMIT 5"
					)
					samples = [str(sample["value"]) for sample in sample_rows]
					if samples:
						blueprint += f"Sample `{text_col}`: {samples}\n"

						# c) Enum-like text columns (best-effort): count distinct values up to 10.
						try:
							cnt_row = await connection.fetchrow(
								f"SELECT COUNT(*)::int AS cnt FROM ("
								f"SELECT DISTINCT {quoted_col} AS v FROM {quoted_schema}.{quoted_table} "
								f"WHERE {quoted_col} IS NOT NULL LIMIT 10"
								f") s"
							)
							cnt = int(cnt_row["cnt"]) if cnt_row and cnt_row["cnt"] is not None else 10
							if cnt < 10:
								table_hint_lines.append(f"Allowed values for {text_col}: {samples}")
								table_hint_lines.append("Use exact match or ILIKE only with these values")
						except Exception:
							pass
				except Exception:
					# Best effort enrichment; skip sample extraction failures silently.
					pass

			# d) Completion hint based on child table name
			child_table_base = table.lower()
			if any(k in child_table_base for k in completion_table_keywords):
				for fk in fk_details:
					if fk["src_table"] != table_name:
						continue
					table_hint_lines.append(
						f"Completion hint: presence in {fk['src_table']} means the record in {fk['dst_table']} is complete"
					)

			# e) Reference columns hint (only if there is a FK)
			for col_meta in info.get("column_meta", []):
				col_name = col_meta["name"]
				if col_name.lower() not in reference_column_names:
					continue
				fk = fk_by_table_col.get((table_name, col_name))
				if not fk:
					continue
				table_hint_lines.append(
					f"Reference: {col_name} links to {fk['dst_table']} via FK -- use JOIN for human-readable names"
				)

			if table_hint_lines:
				for line in table_hint_lines:
					blueprint += f"{line}\n"
				auto_hints_lines.extend(table_hint_lines)

			blueprint += "\n"

		nullable_date_sql = """
		SELECT table_name, column_name, data_type
		FROM information_schema.columns
		WHERE table_schema = 'public'
		AND is_nullable = 'YES'
		AND data_type IN (
			'date', 'timestamp', 'timestamp without time zone',
			'timestamp with time zone', 'timestamptz'
		)
		AND table_name NOT IN (
			'pg_stat_statements', 'alembic_version'
		)
		ORDER BY table_name, column_name
		"""
		nullable_date_rows = await connection.fetch(nullable_date_sql)

		for r in nullable_date_rows:
			hint = f"Status hint: {r['table_name']}.{r['column_name']} IS NULL = pending/incomplete, IS NOT NULL = done/complete"
			auto_hints_lines.append(hint)

		pending_rule = "PENDING RULE: When user asks about pending, incomplete, or not done records — check the Status hints below first. Use IS NULL on the indicated column instead of filtering by a status value. Only use status column if schema sample values explicitly contain the word 'pending'."
		
		auto_hints = pending_rule + "\n" + "\n".join(auto_hints_lines).strip()
		return blueprint.strip(), auto_hints
	except Exception as e:
		raise ValueError(f"Failed to extract database blueprint: {_describe_connection_exception(e)}")
	finally:
		if connection is not None:
			await connection.close()


async def refresh_schema_blueprint(tenant_id: uuid.UUID | str) -> str:
	if session_factory is None:
		raise RuntimeError("DATABASE_URL is not configured. Add it to your .env file.")

	tenant_uuid = uuid.UUID(str(tenant_id))
	async with session_factory() as session:
		statement = select(TenantDBCredential).where(TenantDBCredential.tenant_id == tenant_uuid)
		result = await session.execute(statement)
		credential = result.scalar_one_or_none()
		if credential is None:
			raise ValueError("Tenant credentials not found.")
		if credential.db_type.lower() != "postgresql":
			raise ValueError("Schema refresh is supported only for PostgreSQL tenants.")
		connection_url = _decrypt_credential_value(credential.connection_url)

	blueprint, auto_hints = await fetch_postgres_schema(connection_url)

	async with session_factory() as session:
		statement = select(TenantDBCredential).where(TenantDBCredential.tenant_id == tenant_uuid)
		result = await session.execute(statement)
		credential = result.scalar_one_or_none()
		if credential is None:
			raise ValueError("Tenant credentials not found.")
		credential.schema_blueprint = blueprint
		credential.auto_schema_hints = auto_hints
		await session.commit()

	return blueprint


async def store_query_example(
    tenant_id: uuid.UUID | str,
    question: str,
    sql: str,
    product_connection_id: uuid.UUID | str | None = None,
    verified_by: str = "auto",
) -> uuid.UUID | None:
    if session_factory is None:
        logger.warning("store_query_example: DATABASE_URL not configured, skipping.")
        return None

    from .embeddings import embed_text

    embedding = await embed_text(question)
    if embedding is None:
        logger.warning("store_query_example: embedding failed for question=%r, skipping store.", question[:80])
        return None

    tenant_uuid = uuid.UUID(str(tenant_id))
    product_conn_uuid = uuid.UUID(str(product_connection_id)) if product_connection_id else None
    # Pass embedding as a vector-castable string literal; avoids needing asyncpg register_vector.
    embedding_str = "[" + ",".join(str(v) for v in embedding) + "]"

    async with session_factory() as session:
        # Upsert: increment success_count if same (tenant, question) already exists.
        find_stmt = text(
            "SELECT id FROM tenant_query_examples "
            "WHERE tenant_id = :tenant_id "
            "AND LOWER(TRIM(question)) = LOWER(TRIM(:question)) "
            "LIMIT 1"
        )
        result = await session.execute(find_stmt, {"tenant_id": tenant_uuid, "question": question})
        existing_id = result.scalar_one_or_none()

        if existing_id is not None:
            update_stmt = text(
                "UPDATE tenant_query_examples "
                "SET success_count = success_count + 1, last_used_at = NOW() "
                "WHERE id = :id"
            )
            await session.execute(update_stmt, {"id": existing_id})
            await session.commit()
            return existing_id

        insert_stmt = text(
            "INSERT INTO tenant_query_examples "
            "(tenant_id, product_connection_id, question, sql, question_embedding, verified_by) "
            "VALUES (:tenant_id, :product_connection_id, :question, :sql, "
            "CAST(:embedding AS vector), :verified_by) "
            "RETURNING id"
        )
        insert_result = await session.execute(
            insert_stmt,
            {
                "tenant_id": tenant_uuid,
                "product_connection_id": product_conn_uuid,
                "question": question,
                "sql": sql,
                "embedding": embedding_str,
                "verified_by": verified_by,
            },
        )
        new_id = insert_result.scalar_one()
        await session.commit()
        return new_id


async def retrieve_similar_examples(
    tenant_id: uuid.UUID | str,
    question: str,
    product_connection_id: uuid.UUID | str | None = None,
    limit: int = 5,
) -> list[dict]:
    if session_factory is None:
        return []

    from .embeddings import embed_text

    embedding = await embed_text(question)
    if embedding is None:
        logger.warning("retrieve_similar_examples: embedding failed, returning empty for tenant=%s.", tenant_id)
        return []

    tenant_uuid = uuid.UUID(str(tenant_id))
    product_conn_uuid = uuid.UUID(str(product_connection_id)) if product_connection_id else None
    embedding_str = "[" + ",".join(str(v) for v in embedding) + "]"

    if product_conn_uuid is not None:
        scope_filter = "AND product_connection_id = :product_connection_id "
    else:
        scope_filter = ""

    query = text(
        "SELECT question, sql, "
        "1 - (question_embedding <=> CAST(:embedding AS vector)) AS similarity "
        "FROM tenant_query_examples "
        "WHERE tenant_id = :tenant_id "
        + scope_filter +
        "ORDER BY question_embedding <=> CAST(:embedding AS vector) "
        "LIMIT :limit"
    )

    params: dict = {
        "tenant_id": tenant_uuid,
        "embedding": embedding_str,
        "limit": limit,
    }
    if product_conn_uuid is not None:
        params["product_connection_id"] = product_conn_uuid

    async with session_factory() as session:
        result = await session.execute(query, params)
        rows = result.mappings().all()

    return [
        {"question": row["question"], "sql": row["sql"], "similarity": float(row["similarity"])}
        for row in rows
        if float(row["similarity"]) >= 0.5
    ]


async def deactivate_stale_examples(tenant_id: uuid.UUID | str, days: int = 90) -> int:
    # TODO: add is_active boolean column to tenant_query_examples and implement soft-delete here.
    logger.info("deactivate_stale_examples: not yet implemented (no is_active column).")
    return 0


def _infer_column_type(values: list[str]) -> str:
	"""Infer column type from non-empty cell values."""
	import re as _re
	non_empty = [v for v in values if v not in (None, "")]
	if not non_empty:
		return "text"
	bool_set = {"true", "false", "yes", "no", "1", "0"}
	if all(str(v).strip().lower() in bool_set for v in non_empty):
		return "boolean"
	if all(_re.fullmatch(r"-?\d+", str(v).strip()) for v in non_empty):
		return "integer"
	if all(_re.fullmatch(r"-?\d+\.?\d*", str(v).strip()) for v in non_empty):
		return "numeric"
	date_pat = _re.compile(
		r"^\d{4}[-/]\d{1,2}[-/]\d{1,2}|^\d{1,2}[-/]\d{1,2}[-/]\d{2,4}"
	)
	if all(date_pat.match(str(v).strip()) for v in non_empty):
		return "date"
	return "text"


def _compact_sheet_value(value: Any, max_length: int = 160) -> str:
	cleaned = str(value or "").replace("\n", " ").strip()
	if len(cleaned) <= max_length:
		return cleaned
	return cleaned[: max_length - 1].rstrip() + "…"


def _describe_sheet_from_headers(title: str, headers: list[str]) -> str:
	lowered_title = title.lower()
	lowered_headers = " ".join(headers).lower()
	text = f"{lowered_title} {lowered_headers}"

	if any(word in text for word in ("employee", "staff", "department", "designation", "manager", "salary")):
		return "Employee/HR records, useful for employee lookup, departments, managers, leave, and performance questions."
	if any(word in text for word in ("leave", "absence", "vacation")):
		return "Leave tracking records, useful for leave balance, leaves taken, upcoming leave, and leave reasons."
	if any(word in text for word in ("task", "pending", "deadline", "completion", "rating", "project")):
		return "Task and performance records, useful for pending work, completed tasks, deadlines, and ratings."
	if any(word in text for word in ("dashboard", "metric", "kpi", "summary")):
		return "Dashboard or KPI summary sheet, useful for high-level business metrics."
	return "General worksheet data. Use headers and row values to determine whether it answers the question."


def _important_sheet_columns(headers: list[str], col_types: dict[str, str]) -> list[str]:
	keywords = (
		"id",
		"name",
		"email",
		"phone",
		"department",
		"status",
		"manager",
		"date",
		"leave",
		"task",
		"pending",
		"deadline",
		"rating",
		"amount",
		"salary",
		"total",
		"count",
		"balance",
	)
	important = [
		header
		for header in headers
		if col_types.get(header) in {"integer", "numeric", "date", "boolean"}
		or any(keyword in header.lower() for keyword in keywords)
	]
	return important[:12]


def fetch_google_sheet_data(sheet_id: str, credentials_json: str) -> tuple[str, str]:
	"""Connect to Google Sheets and return (rich_blueprint, hints) strings.

	blueprint — structured schema with inferred types, sample values, and
	            a bounded live data snapshot
	hints     — auto-inferred rules for the LLM (IS NULL patterns, boolean flags,
	            allowed values per column, and semantic sheet descriptions)
	"""
	import json
	import gspread
	from google.oauth2.service_account import Credentials

	CATEGORICAL_THRESHOLD = 25
	SAMPLE_ROWS = 3
	FULL_DATA_ROW_LIMIT = int(os.getenv("GOOGLE_SHEETS_CONTEXT_ROW_LIMIT", "200"))

	scopes = [
		"https://www.googleapis.com/auth/spreadsheets.readonly",
		"https://www.googleapis.com/auth/drive.readonly",
	]

	creds_dict = json.loads(credentials_json)
	creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
	client = gspread.authorize(creds)
	spreadsheet = client.open_by_key(sheet_id)

	blueprint_lines: list[str] = []
	hint_lines: list[str] = []
	sheet_summaries: list[str] = []

	# Skip non-data utility tabs (case-insensitive)
	SKIP_TABS = {"readme", "instructions", "config"}

	for worksheet in spreadsheet.worksheets():
		title = worksheet.title
		if title.strip().lower() in SKIP_TABS:
			continue

		all_values = worksheet.get_all_values()
		if not all_values or len(all_values) < 1:
			blueprint_lines.append(f"Sheet `{title}` | (empty)")
			continue

		headers = [h.strip() for h in all_values[0]]
		data_rows = all_values[1:]

		# Drop blank headers
		valid_indices = [i for i, h in enumerate(headers) if h]
		valid_headers = [headers[i] for i in valid_indices]

		if not valid_headers:
			continue

		# Build column-wise value lists
		col_values: dict[str, list[str]] = {}
		for idx, h in zip(valid_indices, valid_headers):
			col_values[h] = [
				row[idx].strip() if idx < len(row) else ""
				for row in data_rows
			]

		# ── Type inference ──
		col_types: dict[str, str] = {h: _infer_column_type(col_values[h]) for h in valid_headers}

		row_count = len(data_rows)
		sheet_description = _describe_sheet_from_headers(title, valid_headers)
		important_columns = _important_sheet_columns(valid_headers, col_types)
		sheet_summaries.append(f"- `{title}`: {sheet_description}")
		hint_lines.append(f"Sheet `{title}`: {sheet_description}")
		if important_columns:
			hint_lines.append(f"Important columns in `{title}`: {important_columns}")

		blueprint_lines.append(f"Sheet `{title}` | Rows: ~{row_count}")
		blueprint_lines.append(f"Description: {sheet_description}")

		col_parts: list[str] = []
		for h in valid_headers:
			col_parts.append(f"{h} ({col_types[h]})")
		blueprint_lines.append(f"Columns: {', '.join(col_parts)}")
		blueprint_lines.append("Column details:")
		for h in valid_headers:
			nullable = any(v == "" for v in col_values[h]) if data_rows else True
			blueprint_lines.append(f"- `{h}` | type={col_types[h]} | nullable={nullable}")

		# ── Sample values for text columns ──
		for h in valid_headers:
			if col_types[h] != "text":
				continue
			non_empty = [v for v in col_values[h] if v][:5]
			if non_empty:
				blueprint_lines.append(f"Sample `{h}`: {non_empty}")

		# ── Categorical allowed-values ──
		for h in valid_headers:
			if col_types[h] not in ("text", "boolean"):
				continue
			distinct = sorted({v for v in col_values[h] if v})
			if 0 < len(distinct) <= CATEGORICAL_THRESHOLD:
				blueprint_lines.append(f"Allowed values for `{h}`: {distinct}")
				hint_lines.append(f"Allowed values for `{h}`: {distinct} — use exact match or case-insensitive contains")

		# ── Date/nullable column hints (IS NULL = pending pattern) ──
		status_keywords = {
			"completed", "done", "approved", "closed", "finished",
			"resolved", "verified", "paid", "delivered", "submission",
		}
		for h in valid_headers:
			if col_types[h] != "date":
				continue
			has_empty = any(v == "" for v in col_values[h])
			if has_empty and any(k in h.lower() for k in status_keywords):
				hint_lines.append(
					f"Status hint: Sheet `{title}` column `{h}` empty = pending/incomplete, "
					f"IS NOT NULL = done/complete"
				)

		# ── Boolean hints ──
		for h in valid_headers:
			if col_types[h] == "boolean":
				hint_lines.append(f"Boolean column: `{h}` — compare with TRUE/FALSE/Yes/No, never use ILIKE")

		# ── Sample data rows (first N) ──
		if data_rows:
			blueprint_lines.append(f"Sample data ({min(SAMPLE_ROWS, row_count)} rows):")
			for row in data_rows[:SAMPLE_ROWS]:
				row_dict = {
					h: _compact_sheet_value(row[i] if i < len(row) else "")
					for i, h in zip(valid_indices, valid_headers)
				}
				blueprint_lines.append(f"  {row_dict}")

			visible_rows = data_rows[:FULL_DATA_ROW_LIMIT]
			blueprint_lines.append(
				f"Full data snapshot ({len(visible_rows)} of {row_count} rows):"
			)
			for row_number, row in enumerate(visible_rows, start=2):
				row_dict = {
					h: _compact_sheet_value(row[i] if i < len(row) else "")
					for i, h in zip(valid_indices, valid_headers)
				}
				blueprint_lines.append(f"  Row {row_number}: {row_dict}")
			if row_count > len(visible_rows):
				blueprint_lines.append(
					f"  Snapshot truncated: {row_count - len(visible_rows)} additional rows are not included."
				)

		blueprint_lines.append("")

	semantic_schema = (
		"Semantic schema summary:\n"
		+ ("\n".join(sheet_summaries) if sheet_summaries else "- No non-empty worksheets found.")
	)
	blueprint = "Database Blueprint (Google Sheets):\n" + semantic_schema + "\n\n" + "\n".join(blueprint_lines)

	pending_rule = (
		"PENDING RULE: When the user asks about pending, incomplete, or not done records — "
		"check Status hints first. Use empty/blank check on the indicated column instead of "
		"filtering by a text value. Only filter by text if the Allowed values list explicitly "
		"contains the word 'pending'."
	)
	hints = pending_rule + "\n" + "\n".join(hint_lines)
	return blueprint, hints
