from __future__ import annotations

import os
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
		connection = await asyncpg.connect(
			connection_string,
			ssl="require" if credential.ssl_required else "prefer",
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
	except asyncpg.PostgresError:
		raise QueryExecutionError("Failed to execute query against tenant database.")
	except Exception:
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
	try:
		connection = await asyncpg.connect(connection_string, timeout=10)
		sql = """
		SELECT table_name, column_name, data_type
		FROM information_schema.columns
		WHERE table_schema = 'public'
		ORDER BY table_name, ordinal_position;
		"""
		rows = await connection.fetch(sql)
		await connection.close()
		
		tables = {}
		for row in rows:
			t = row['table_name']
			c = row['column_name']
			d = row['data_type']
			if t not in tables:
				tables[t] = []
			tables[t].append(f"{c} ({d})")
			
		blueprint = "Database Blueprint (PostgreSQL):\n"
		for t, cols in tables.items():
			blueprint += f"Table `{t}` | Columns: {', '.join(cols)}\n"
			
		return blueprint.strip()
	except Exception as e:
		raise ValueError(f"Failed to extract database blueprint: {e}")


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
