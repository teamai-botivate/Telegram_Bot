from __future__ import annotations

import os
from typing import Any

import asyncpg
from dotenv import load_dotenv
from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, Field

from .database import encrypt_credential_value, save_schema_map, save_tenant_credentials

load_dotenv()

ADMIN_SECRET_TOKEN = os.getenv("ADMIN_SECRET_TOKEN", "")

router = APIRouter(prefix="/admin/tenant", tags=["admin"])


class ConnectTenantDBRequest(BaseModel):
    tenant_id: str
    db_type: str = Field(min_length=1)
    host: str = Field(min_length=1)
    port: int = Field(gt=0)
    database_name: str = Field(min_length=1)
    db_user: str = Field(min_length=1)
    db_password: str = Field(min_length=1)
    ssl_required: bool = True


class SaveSchemaMapRequest(BaseModel):
    tenant_id: str
    module: str = Field(min_length=1)
    intent: str = Field(min_length=1)
    sql_template: str = Field(min_length=1)


async def verify_admin_token(x_admin_token: str | None = Header(default=None, alias="x-admin-token")) -> None:
    if not ADMIN_SECRET_TOKEN:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="ADMIN_SECRET_TOKEN is not configured.",
        )

    if x_admin_token != ADMIN_SECRET_TOKEN:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid admin token.")


@router.post("/connect-db")
async def connect_tenant_db(
    payload: ConnectTenantDBRequest,
    _: None = Depends(verify_admin_token),
) -> dict[str, Any]:
    if payload.db_type.lower() != "postgresql":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only postgresql is supported for connection testing right now.",
        )

    try:
        connection = await asyncpg.connect(
            host=payload.host,
            port=payload.port,
            user=payload.db_user,
            password=payload.db_password,
            database=payload.database_name,
            ssl="require" if payload.ssl_required else "prefer",
            timeout=5,
        )
        await connection.close()
    except Exception:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Could not connect to tenant database.")

    encrypted_fields = {
        "host": encrypt_credential_value(payload.host),
        "port": encrypt_credential_value(str(payload.port)),
        "database_name": encrypt_credential_value(payload.database_name),
        "db_user": encrypt_credential_value(payload.db_user),
        "db_password": encrypt_credential_value(payload.db_password),
    }

    try:
        await save_tenant_credentials(
            tenant_id=payload.tenant_id,
            db_type=payload.db_type,
            encrypted_fields=encrypted_fields,
            ssl_required=payload.ssl_required,
        )
    except ValueError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found.")

    return {"status": "connected"}


@router.post("/schema-map")
async def upsert_schema_map(
    payload: SaveSchemaMapRequest,
    _: None = Depends(verify_admin_token),
) -> dict[str, str]:
    try:
        await save_schema_map(
            tenant_id=payload.tenant_id,
            module=payload.module,
            intent=payload.intent,
            sql_template=payload.sql_template,
        )
    except ValueError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found.")

    return {"status": "saved"}


import jwt
from datetime import datetime, timedelta, timezone
from .database import create_tenant_record


class CreateFullTenantRequest(BaseModel):
    company_name: str
    active_modules: list[str]
    db_type: str = Field(min_length=1)
    host: str = Field(min_length=1)
    port: int = Field(gt=0)
    database_name: str = Field(min_length=1)
    db_user: str = Field(min_length=1)
    db_password: str = Field(min_length=1)
    ssl_required: bool = True
    schema_maps: list[dict[str, str]]


@router.post("/create-full")
async def create_full_tenant(
    payload: CreateFullTenantRequest,
    _: None = Depends(verify_admin_token),
) -> dict[str, Any]:
    if payload.db_type.lower() != "postgresql":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only postgresql is supported for connection testing right now.",
        )

    try:
        connection = await asyncpg.connect(
            host=payload.host,
            port=payload.port,
            user=payload.db_user,
            password=payload.db_password,
            database=payload.database_name,
            ssl="require" if payload.ssl_required else "prefer",
            timeout=5,
        )
        await connection.close()
    except Exception:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Could not connect to tenant database.")

    # 1. Create the tenant
    tenant_id = await create_tenant_record(payload.company_name, payload.active_modules)

    # 2. Save credentials
    encrypted_fields = {
        "host": encrypt_credential_value(payload.host),
        "port": encrypt_credential_value(str(payload.port)),
        "database_name": encrypt_credential_value(payload.database_name),
        "db_user": encrypt_credential_value(payload.db_user),
        "db_password": encrypt_credential_value(payload.db_password),
    }

    await save_tenant_credentials(
        tenant_id=tenant_id,
        db_type=payload.db_type,
        encrypted_fields=encrypted_fields,
        ssl_required=payload.ssl_required,
    )

    # 3. Save schemas
    for schema_map in payload.schema_maps:
        await save_schema_map(
            tenant_id=tenant_id,
            module=schema_map["module"],
            intent=schema_map["intent"],
            sql_template=schema_map["sql_template"],
        )

    return {"status": "created", "tenant_id": str(tenant_id)}


@router.post("/{tenant_id}/generate-link")
async def generate_magic_link(
    tenant_id: str,
    _: None = Depends(verify_admin_token),
) -> dict[str, str]:
    if not ADMIN_SECRET_TOKEN:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="ADMIN_SECRET_TOKEN is not configured for JWT signing.",
        )

    payload = {
        "tenant_id": tenant_id,
        "exp": datetime.now(timezone.utc) + timedelta(hours=72)
    }
    encoded_jwt = jwt.encode(payload, ADMIN_SECRET_TOKEN, algorithm="HS256")

    # The portal endpoint is static/portal.html so we assume the same host that the API is hitting.
    return {"token": encoded_jwt}
