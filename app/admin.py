from __future__ import annotations

import os
from typing import Any

import asyncpg
import jwt
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, Field

from .database import create_tenant_record, fetch_postgres_schema, save_tenant_credentials

load_dotenv()

ADMIN_SECRET_TOKEN = os.getenv("ADMIN_SECRET_TOKEN", "")

router = APIRouter(prefix="/admin/tenant", tags=["admin"])


class ConnectTenantDBRequest(BaseModel):
    tenant_id: str
    db_type: str = Field(min_length=1)  # e.g., postgresql or google_sheets
    connection_url: str = Field(min_length=1)
    ssl_required: bool = True


class CreateFullTenantRequest(BaseModel):
    company_name: str
    active_modules: list[str]
    db_type: str = Field(min_length=1)
    connection_url: str = Field(min_length=1)
    ssl_required: bool = True


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
    schema_blueprint = None

    if payload.db_type.lower() == "postgresql":
        try:
            schema_blueprint = await fetch_postgres_schema(payload.connection_url)
        except Exception as e:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Database execution failed: {str(e)}")
    elif payload.db_type.lower() == "google_sheets":
        schema_blueprint = "Google Sheets Auto-Discovery Placeholder"
    else:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Unsupported DB type.")

    try:
        await save_tenant_credentials(
            tenant_id=payload.tenant_id,
            db_type=payload.db_type,
            connection_url=payload.connection_url,
            schema_blueprint=schema_blueprint,
            ssl_required=payload.ssl_required,
        )
    except ValueError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found.")

    return {"status": "connected"}


@router.post("/create-full")
async def create_full_tenant(
    payload: CreateFullTenantRequest,
    _: None = Depends(verify_admin_token),
) -> dict[str, Any]:
    schema_blueprint = None

    if payload.db_type.lower() == "postgresql":
        try:
            schema_blueprint = await fetch_postgres_schema(payload.connection_url)
        except Exception as e:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Database connection failed: {str(e)}")
    elif payload.db_type.lower() == "google_sheets":
        schema_blueprint = "Google Sheets Pending Fetch"
    else:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Unsupported DB type.")

    tenant_id = await create_tenant_record(payload.company_name, payload.active_modules)

    await save_tenant_credentials(
        tenant_id=tenant_id,
        db_type=payload.db_type,
        connection_url=payload.connection_url,
        schema_blueprint=schema_blueprint,
        ssl_required=payload.ssl_required,
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

    payload_data = {
        "tenant_id": tenant_id,
        "exp": datetime.now(timezone.utc) + timedelta(hours=72)
    }
    encoded_jwt = jwt.encode(payload_data, ADMIN_SECRET_TOKEN, algorithm="HS256")
    return {"token": encoded_jwt}
