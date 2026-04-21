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
