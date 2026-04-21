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

from .models import Base, Tenant, TenantDBCredential, TenantSchemaMap

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


async def get_sql_template(tenant_id: uuid.UUID | str, module: str, intent: str) -> str | None:
	if session_factory is None:
		raise RuntimeError("DATABASE_URL is not configured. Add it to your .env file.")

	tenant_uuid = uuid.UUID(str(tenant_id))
	statement = select(TenantSchemaMap.sql_template).where(
		TenantSchemaMap.tenant_id == tenant_uuid,
		TenantSchemaMap.module == module,
		TenantSchemaMap.intent == intent,
	)

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
	encrypted_fields: dict[str, str],
	ssl_required: bool = True,
	schema_map: dict[str, str] | None = None,
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

		if credential is None:
			credential = TenantDBCredential(
				tenant_id=tenant_uuid,
				db_type=db_type,
				host=encrypted_fields["host"],
				port=encrypted_fields["port"],
				database_name=encrypted_fields["database_name"],
				db_user=encrypted_fields["db_user"],
				db_password=encrypted_fields["db_password"],
				ssl_required=ssl_required,
				schema_map=schema_map,
			)
			session.add(credential)
		else:
			credential.db_type = db_type
			credential.host = encrypted_fields["host"]
			credential.port = encrypted_fields["port"]
			credential.database_name = encrypted_fields["database_name"]
			credential.db_user = encrypted_fields["db_user"]
			credential.db_password = encrypted_fields["db_password"]
			credential.ssl_required = ssl_required
			credential.schema_map = schema_map

		await session.commit()
		await session.refresh(credential)

		return credential


async def save_schema_map(tenant_id: uuid.UUID | str, module: str, intent: str, sql_template: str) -> TenantSchemaMap:
	if session_factory is None:
		raise RuntimeError("DATABASE_URL is not configured. Add it to your .env file.")

	tenant_uuid = uuid.UUID(str(tenant_id))

	async with session_factory() as session:
		tenant = await session.get(Tenant, tenant_uuid)
		if tenant is None:
			raise ValueError("Tenant not found.")

		statement = select(TenantSchemaMap).where(
			TenantSchemaMap.tenant_id == tenant_uuid,
			TenantSchemaMap.module == module,
			TenantSchemaMap.intent == intent,
		)
		existing_result = await session.execute(statement)
		schema_map_record = existing_result.scalar_one_or_none()

		if schema_map_record is None:
			schema_map_record = TenantSchemaMap(
				tenant_id=tenant_uuid,
				module=module,
				intent=intent,
				sql_template=sql_template,
			)
			session.add(schema_map_record)
		else:
			schema_map_record.sql_template = sql_template

		await session.commit()
		await session.refresh(schema_map_record)

		return schema_map_record


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
		host = _decrypt_credential_value(credential.host)
		port = int(_decrypt_credential_value(credential.port))
		database_name = _decrypt_credential_value(credential.database_name)
		db_user = _decrypt_credential_value(credential.db_user)
		db_password = _decrypt_credential_value(credential.db_password)
	except (InvalidToken, ValueError):
		raise TenantDBConnectionError("Tenant database credentials are invalid or could not be decrypted.")

	try:
		connection = await asyncpg.connect(
			host=host,
			port=port,
			user=db_user,
			password=db_password,
			database=database_name,
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
