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
from .security import encrypt_credential_value
async def create_tables() -> None:
	if meta_engine is None:
		raise RuntimeError("DATABASE_URL is not configured. Add it to your .env file.")

	async with meta_engine.begin() as connection:
		await connection.run_sync(Base.metadata.create_all)

async def find_registered_client_by_chat(
    platform: str,
    chat_id: str,
    phone: str | None = None,
) -> RegisteredClient | None:
    """Look up a pre-registered (not yet onboarded) client by their platform handle.

    Telegram: match on telegram_chat_id.
    WhatsApp: match on whatsapp_number OR phone_number (both stored in E.164 format).
    Returns None if not found or the session factory is unconfigured.
    """
    if session_factory is None:
        return None

    platform_lower = platform.lower()

    async with session_factory() as session:
        if platform_lower == "telegram":
            stmt = select(RegisteredClient).where(
                RegisteredClient.telegram_chat_id == chat_id,
                RegisteredClient.is_active.is_(True),
            )
        else:
            # WhatsApp: chat_id is the sender's phone number; also check phone_number column.
            conditions = [RegisteredClient.whatsapp_number == chat_id]
            if phone:
                conditions.append(RegisteredClient.phone_number == phone)
            stmt = select(RegisteredClient).where(
                or_(*conditions),
                RegisteredClient.is_active.is_(True),
            )

        result = await session.execute(stmt)
        return result.scalar_one_or_none()

async def get_tenant_by_chat_id(chat_id: str) -> Tenant | None:
	if session_factory is None:
		raise RuntimeError("DATABASE_URL is not configured. Add it to your .env file.")

	statement = select(Tenant).where(or_(Tenant.telegram_chat_id == chat_id, Tenant.whatsapp_number == chat_id))

	async with session_factory() as session:
		result = await session.execute(statement)
		return result.scalar_one_or_none()

async def get_tenant_credentials(tenant_id: uuid.UUID | str) -> TenantDBCredential | None:
	"""Return a single credential row for a tenant.

	Multi-DB tenants have more than one row; this legacy helper returns the most
	recent one. Callers that need the full set should use `get_tenant_credentials_all`.
	"""
	if session_factory is None:
		raise RuntimeError("DATABASE_URL is not configured. Add it to your .env file.")

	tenant_uuid = uuid.UUID(str(tenant_id))
	statement = (
		select(TenantDBCredential)
		.where(TenantDBCredential.tenant_id == tenant_uuid)
		.order_by(TenantDBCredential.last_connected_at.desc().nullslast())
		.limit(1)
	)

	async with session_factory() as session:
		result = await session.execute(statement)
		return result.scalars().first()

async def get_tenant_credentials_all(tenant_id: uuid.UUID | str) -> list[TenantDBCredential]:
	"""Return every credential row attached to a tenant. Empty list if none."""
	if session_factory is None:
		raise RuntimeError("DATABASE_URL is not configured. Add it to your .env file.")

	tenant_uuid = uuid.UUID(str(tenant_id))
	statement = (
		select(TenantDBCredential)
		.where(TenantDBCredential.tenant_id == tenant_uuid)
		.order_by(TenantDBCredential.last_connected_at.desc().nullslast())
	)

	async with session_factory() as session:
		result = await session.execute(statement)
		return list(result.scalars().all())

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

