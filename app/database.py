from __future__ import annotations

import os
import socket
import uuid
from datetime import datetime, timezone
from typing import Any

import asyncpg
from cryptography.fernet import Fernet, InvalidToken
from dotenv import load_dotenv
from sqlalchemy import or_, select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from .models import Base, Tenant, TenantDBCredential

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "")
FERNET_SECRET_KEY = os.getenv("FERNET_SECRET_KEY", "")


class TenantDBConnectionError(Exception):
	"""Raised when a tenant database connection cannot be established."""


class QueryExecutionError(Exception):
	"""Raised when query execution against tenant DB fails."""


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
				ssl_required=ssl_required,
				google_credentials=encrypted_creds,
			)
			session.add(credential)
		else:
			credential.db_type = db_type
			credential.connection_url = encrypted_url
			if schema_blueprint is not None:
				credential.schema_blueprint = schema_blueprint
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

		parsed = urlparse(connection_string)
		query_params = parse_qs(parsed.query)

		# asyncpg doesn't understand sslmode=; strip it and pass ssl= explicitly
		ssl_mode = query_params.pop("sslmode", query_params.pop("ssl", [None]))[0]
		clean_query = urlencode({k: v[0] for k, v in query_params.items()})
		clean_url = urlunparse(parsed._replace(query=clean_query))

		# Determine SSL setting
		if ssl_mode and ssl_mode in ("require", "prefer", "disable"):
			ssl_arg = ssl_mode
		elif credential.ssl_required:
			ssl_arg = "require"
		else:
			ssl_arg = "prefer"

		connection = await asyncpg.connect(
			clean_url,
			ssl=ssl_arg,
			timeout=10,
		)
		await _touch_last_connected(credential.id)
		return connection
	except Exception:
		raise TenantDBConnectionError("Could not connect to tenant database. Please verify connection settings.")


async def execute_tenant_query(tenant_id: uuid.UUID | str, sql: str, *params: Any) -> list[dict[str, Any]]:
	connection = await decrypt_and_connect(tenant_id)

	try:
		rows = await connection.fetch(sql, *params)
		return [dict(row) for row in rows]
	except asyncpg.PostgresError as e:
		import logging
		logger = logging.getLogger(__name__)
		logger.error(f"PostgresError executing tenant query. SQL: {sql} | Error: {e}")
		raise QueryExecutionError(f"Failed to execute query: {e}")
	except Exception as e:
		import logging
		logger = logging.getLogger(__name__)
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


async def fetch_postgres_schema(connection_string: str) -> str:
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

		sql = """
		SELECT table_schema, table_name, column_name, data_type, udt_name
		FROM information_schema.columns
		WHERE table_schema NOT IN ('information_schema', 'pg_catalog', 'auth', 'storage', 'vault', 'realtime')
		ORDER BY table_schema, table_name, ordinal_position;
		"""
		rows = await connection.fetch(sql)

		tables: dict[str, list[str]] = {}
		for row in rows:
			schema = row["table_schema"]
			table = row["table_name"]
			column = row["column_name"]
			data_type = row["data_type"]
			udt_name = row["udt_name"]

			if data_type == "USER-DEFINED" and udt_name in enums:
				data_type = f"enum({', '.join(enums[udt_name])})"

			full_name = f"{schema}.{table}" if schema != "public" else table
			tables.setdefault(full_name, []).append(f"{column} ({data_type})")

		blueprint = "Database Blueprint (PostgreSQL):\n"
		for table_name, cols in tables.items():
			blueprint += f"Table `{table_name}` | Columns: {', '.join(cols)}\n"

		return blueprint.strip()
	except Exception as e:
		raise ValueError(f"Failed to extract database blueprint: {_describe_connection_exception(e)}")
	finally:
		if connection is not None:
			await connection.close()


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
