from __future__ import annotations

import os
from typing import Any

import jwt
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
from fastapi import APIRouter, Depends, Header, HTTPException, status
from fastapi import Query as FastAPIQuery
from pydantic import BaseModel, Field

from .database import (
    create_or_attach_tenant_record,
    execute_tenant_query,
    fetch_google_sheet_data,
    fetch_postgres_schema,
    refresh_schema_blueprint,
    save_tenant_credentials,
)

load_dotenv()

ADMIN_SECRET_TOKEN = os.getenv("ADMIN_SECRET_TOKEN", "")

router = APIRouter(prefix="/admin/tenant", tags=["admin"])


# ─── Request Models ─────────────────────────────────────────────────────────────

class ConnectTenantDBRequest(BaseModel):
    tenant_id: str
    db_type: str = Field(min_length=1)
    # For PostgreSQL (Supabase / AWS)
    connection_url: str | None = None
    ssl_required: bool = True
    # For Google Sheets
    google_sheet_id: str | None = None
    google_credentials_json: str | None = None


class CreateFullTenantRequest(BaseModel):
    company_name: str
    active_modules: list[str]
    db_type: str = Field(min_length=1)
    # For PostgreSQL (Supabase / AWS)
    connection_url: str | None = None
    ssl_required: bool = True
    # For Google Sheets
    google_sheet_id: str | None = None
    google_credentials_json: str | None = None


# ─── Auth ────────────────────────────────────────────────────────────────────────

async def verify_admin_token(x_admin_token: str | None = Header(default=None, alias="x-admin-token")) -> None:
    if not ADMIN_SECRET_TOKEN:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="ADMIN_SECRET_TOKEN is not configured.",
        )
    if x_admin_token != ADMIN_SECRET_TOKEN:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid admin token.")


# ─── Helpers ─────────────────────────────────────────────────────────────────────

def _extract_schema_and_creds(db_type: str, payload: ConnectTenantDBRequest | CreateFullTenantRequest) -> tuple[str, str | None, str | None]:
    """Returns (connection_url, schema_blueprint, google_credentials_json)."""
    if db_type.lower() == "postgresql":
        if not payload.connection_url:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="connection_url is required for PostgreSQL.")
        try:
            blueprint = fetch_postgres_schema.__doc__  # placeholder until await below
            return payload.connection_url, None, None  # blueprint filled async by caller
        except Exception as e:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Database error: {e}")

    if db_type.lower() == "google_sheets":
        if not payload.google_sheet_id or not payload.google_credentials_json:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="google_sheet_id and google_credentials_json are required for Google Sheets."
            )
        return f"google_sheets://{payload.google_sheet_id}", None, payload.google_credentials_json

    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Unsupported db_type: '{db_type}'. Use 'postgresql' or 'google_sheets'.")


# ─── Routes ──────────────────────────────────────────────────────────────────────

@router.post("/connect-db")
async def connect_tenant_db(
    payload: ConnectTenantDBRequest,
    _: None = Depends(verify_admin_token),
) -> dict[str, Any]:
    schema_blueprint: str | None = None
    auto_schema_hints: str | None = None
    google_credentials: str | None = None
    connection_url = ""

    if payload.db_type.lower() == "postgresql":
        if not payload.connection_url:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="connection_url is required for PostgreSQL.")
        try:
            schema_blueprint, auto_schema_hints = await fetch_postgres_schema(payload.connection_url)
        except Exception as e:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Database connection failed: {e}")
        connection_url = payload.connection_url

    elif payload.db_type.lower() == "google_sheets":
        if not payload.google_sheet_id or not payload.google_credentials_json:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="google_sheet_id and google_credentials_json are required.")
        try:
            blueprint, _ = fetch_google_sheet_data(payload.google_sheet_id, payload.google_credentials_json)
            schema_blueprint = blueprint
        except Exception as e:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Google Sheets connection failed: {e}")
        connection_url = f"google_sheets://{payload.google_sheet_id}"
        google_credentials = payload.google_credentials_json
    else:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Unsupported db_type.")

    try:
        await save_tenant_credentials(
            tenant_id=payload.tenant_id,
            db_type=payload.db_type,
            connection_url=connection_url,
            schema_blueprint=schema_blueprint,
            auto_schema_hints=auto_schema_hints,
            ssl_required=payload.ssl_required if payload.db_type.lower() == "postgresql" else False,
            google_credentials=google_credentials,
        )
    except ValueError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found.")

    return {"status": "connected"}


@router.post("/create-full")
async def create_full_tenant(
    payload: CreateFullTenantRequest,
    _: None = Depends(verify_admin_token),
) -> dict[str, Any]:
    schema_blueprint: str | None = None
    auto_schema_hints: str | None = None
    google_credentials: str | None = None
    connection_url = ""

    if payload.db_type.lower() == "postgresql":
        if not payload.connection_url:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="connection_url is required for PostgreSQL.")
        try:
            schema_blueprint, auto_schema_hints = await fetch_postgres_schema(payload.connection_url)
        except Exception as e:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Database connection failed: {e}")
        connection_url = payload.connection_url

    elif payload.db_type.lower() == "google_sheets":
        if not payload.google_sheet_id or not payload.google_credentials_json:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="google_sheet_id and google_credentials_json are required.")
        try:
            blueprint, _ = fetch_google_sheet_data(payload.google_sheet_id, payload.google_credentials_json)
            schema_blueprint = blueprint
        except Exception as e:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Google Sheets connection failed: {e}")
        connection_url = f"google_sheets://{payload.google_sheet_id}"
        google_credentials = payload.google_credentials_json
    else:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Unsupported db_type.")

    tenant_id, attached_to_existing = await create_or_attach_tenant_record(payload.company_name, payload.active_modules)

    await save_tenant_credentials(
        tenant_id=tenant_id,
        db_type=payload.db_type,
        connection_url=connection_url,
        schema_blueprint=schema_blueprint,
        auto_schema_hints=auto_schema_hints,
        ssl_required=payload.ssl_required if payload.db_type.lower() == "postgresql" else False,
        google_credentials=google_credentials,
    )

    if attached_to_existing:
        return {
            "status": "attached",
            "tenant_id": str(tenant_id),
            "message": "Existing company tenant reused and modules merged.",
        }

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


@router.post("/{tenant_id}/refresh-schema")
async def refresh_tenant_schema(
    tenant_id: str,
    _: None = Depends(verify_admin_token),
) -> dict[str, Any]:
    try:
        blueprint = await refresh_schema_blueprint(tenant_id)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Failed to refresh schema blueprint: {e}")

    return {
        "status": "refreshed",
        "tenant_id": tenant_id,
        "schema_blueprint_preview": blueprint[:500],
    }


@router.get("/{tenant_id}/test-query")
async def test_tenant_query(
    tenant_id: str,
    q: str = FastAPIQuery(..., description="SQL query to test"),
    _: None = Depends(verify_admin_token),
) -> dict[str, Any]:
    try:
        rows = await execute_tenant_query(tenant_id, q, allow_select_star=True)
        return {"sql": q, "rows": rows, "error": None}
    except Exception as e:
        return {"sql": q, "rows": [], "error": str(e)}
