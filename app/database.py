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
from sqlalchemy import func, or_, select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from .models import Base, Tenant, TenantDBCredential

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "")
FERNET_SECRET_KEY = os.getenv("FERNET_SECRET_KEY", "")
TENANT_DB_CONNECT_TIMEOUT_SECONDS = float(os.getenv("TENANT_DB_CONNECT_TIMEOUT_SECONDS", "20"))
TENANT_DB_CONNECT_RETRIES = int(os.getenv("TENANT_DB_CONNECT_RETRIES", "1"))
logger = logging.getLogger(__name__)


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


async def decrypt_and_connect(tenant_id: uuid.UUID | str) -> asyncpg.Connection:
	credential = await get_tenant_credentials(tenant_id)
	if credential is None:
		raise TenantDBConnectionError("Tenant database is not configured yet.")

	if credential.db_type.lower() != "postgresql":
		raise TenantDBConnectionError("Only PostgreSQL tenant databases are currently supported.")

	try:
		connection_string = _decrypt_credential_value(credential.connection_url)
	except (InvalidToken, ValueError):
		raise TenantDBConnectionError("Tenant database credentials are invalid or could not be decrypted.")

	try:
		from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

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


async def execute_tenant_query(
	tenant_id: uuid.UUID | str,
	sql: str,
	*params: Any,
	allow_select_star: bool = False,
) -> list[dict[str, Any]]:
	connection = await decrypt_and_connect(tenant_id)

	try:
		safe_sql = _sanitize_select_sql(sql, allow_select_star=allow_select_star)
		logger.info(
			"Tenant query attempt tenant_id=%s timestamp=%s sql=%s",
			tenant_id,
			datetime.now(timezone.utc).isoformat(),
			safe_sql,
		)

		rows = await connection.fetch(safe_sql, *params)
		return [dict(row) for row in rows]
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
	finally:
		await connection.close()


async def create_tenant_record(company_name: str, active_modules: list[str]) -> uuid.UUID:
	if session_factory is None:
		raise RuntimeError("DATABASE_URL is not configured.")

	tenant = Tenant(company_name=company_name, active_modules=active_modules)
	async with session_factory() as session:
		session.add(tenant)
		await session.commit()
		await session.refresh(tenant)
		return tenant.id


def _normalize_modules(active_modules: list[str]) -> list[str]:
	seen: set[str] = set()
	normalized: list[str] = []
	for module in active_modules:
		cleaned = module.strip()
		if not cleaned:
			continue
		key = cleaned.lower()
		if key in seen:
			continue
		seen.add(key)
		normalized.append(cleaned)
	return normalized


async def create_or_attach_tenant_record(company_name: str, active_modules: list[str]) -> tuple[uuid.UUID, bool]:
	"""
	Create a tenant when company is new, otherwise reuse existing tenant and merge modules.

	Returns: (tenant_id, attached_to_existing)
	"""
	if session_factory is None:
		raise RuntimeError("DATABASE_URL is not configured.")

	normalized_company = company_name.strip()
	if not normalized_company:
		raise ValueError("company_name is required.")

	requested_modules = _normalize_modules(active_modules)

	async with session_factory() as session:
		statement = (
			select(Tenant)
			.where(func.lower(Tenant.company_name) == normalized_company.lower())
			.order_by(Tenant.created_at.asc())
		)
		result = await session.execute(statement)
		company_tenants = list(result.scalars().all())

		existing = None
		for tenant in company_tenants:
			if tenant.telegram_chat_id or tenant.whatsapp_number:
				existing = tenant
				break
		if existing is None and company_tenants:
			existing = company_tenants[0]

		if existing is None:
			tenant = Tenant(company_name=normalized_company, active_modules=requested_modules)
			session.add(tenant)
			await session.commit()
			await session.refresh(tenant)
			return tenant.id, False

		merged = _normalize_modules([*(existing.active_modules or []), *requested_modules])
		if merged != (existing.active_modules or []):
			existing.active_modules = merged
			await session.commit()

		return existing.id, True


async def update_tenant_chat_id(tenant_id: uuid.UUID | str, platform: str, chat_id: str) -> None:
	if session_factory is None:
		return

	tenant_uuid = uuid.UUID(str(tenant_id))
	async with session_factory() as session:
		tenant = await session.get(Tenant, tenant_uuid)
		if tenant is None:
			raise ValueError("Tenant not found.")

		if platform.lower() == "telegram":
			existing_stmt = select(Tenant).where(Tenant.telegram_chat_id == chat_id)
			existing_result = await session.execute(existing_stmt)
			existing = existing_result.scalars().first()
			if existing is not None and existing.id != tenant.id:
				raise ValueError(
					"This Telegram chat is already linked to another service. "
					"Add the new module to the existing company tenant instead of creating a separate tenant."
				)
			tenant.telegram_chat_id = chat_id
		elif platform.lower() == "whatsapp":
			existing_stmt = select(Tenant).where(Tenant.whatsapp_number == chat_id)
			existing_result = await session.execute(existing_stmt)
			existing = existing_result.scalars().first()
			if existing is not None and existing.id != tenant.id:
				raise ValueError(
					"This WhatsApp number is already linked to another service. "
					"Add the new module to the existing company tenant instead of creating a separate tenant."
				)
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
		WHERE table_schema NOT IN ('information_schema', 'pg_catalog', 'auth', 'storage', 'vault', 'realtime')
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
			AND tc.table_schema NOT IN ('information_schema', 'pg_catalog', 'auth', 'storage', 'vault', 'realtime')
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

		auto_hints = "\n".join(auto_hints_lines).strip()
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


def fetch_google_sheet_data(sheet_id: str, credentials_json: str) -> tuple[str, str]:
	"""Connect to Google Sheets and return (blueprint, data_snapshot) strings."""
	import json
	import gspread
	from google.oauth2.service_account import Credentials

	scopes = [
		"https://www.googleapis.com/auth/spreadsheets.readonly",
		"https://www.googleapis.com/auth/drive.readonly",
	]

	creds_dict = json.loads(credentials_json)
	creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
	client = gspread.authorize(creds)

	spreadsheet = client.open_by_key(sheet_id)

	blueprint_lines: list[str] = []
	snapshot_lines: list[str] = []

	for worksheet in spreadsheet.worksheets():
		title = worksheet.title
		all_values = worksheet.get_all_values()

		if not all_values:
			blueprint_lines.append(f"Sheet `{title}` | (empty)")
			continue

		headers = all_values[0]
		blueprint_lines.append(f"Sheet `{title}` | Columns: {', '.join(headers)}")

		# Include first 50 rows as a snapshot to give the LLM real data context
		for row in all_values[1:51]:
			row_dict = dict(zip(headers, row))
			snapshot_lines.append(f"{title}: {row_dict}")

	blueprint = "Database Blueprint (Google Sheets):\n" + "\n".join(blueprint_lines)
	snapshot = "\n".join(snapshot_lines)
	return blueprint, snapshot
