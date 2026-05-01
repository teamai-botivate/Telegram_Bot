from __future__ import annotations

import os
import uuid
from typing import Any

import jwt
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
from fastapi import APIRouter, Depends, Header, HTTPException, status
from fastapi import Query as FastAPIQuery
from pydantic import BaseModel, Field

from .bot_logic import _validate_generated_sql
from .database import (
    create_tenant_record,
    execute_tenant_query,
    fetch_google_sheet_data,
    fetch_postgres_schema,
    refresh_schema_blueprint,
    save_tenant_credentials,
    session_factory,
    store_query_example,
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
            blueprint, auto_schema_hints = fetch_google_sheet_data(payload.google_sheet_id, payload.google_credentials_json)
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
            blueprint, auto_schema_hints = fetch_google_sheet_data(payload.google_sheet_id, payload.google_credentials_json)
            schema_blueprint = blueprint
        except Exception as e:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Google Sheets connection failed: {e}")
        connection_url = f"google_sheets://{payload.google_sheet_id}"
        google_credentials = payload.google_credentials_json
    else:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Unsupported db_type.")

    tenant_id = await create_tenant_record(payload.company_name, payload.active_modules)

    await save_tenant_credentials(
        tenant_id=tenant_id,
        db_type=payload.db_type,
        connection_url=connection_url,
        schema_blueprint=schema_blueprint,
        auto_schema_hints=auto_schema_hints,
        ssl_required=payload.ssl_required if payload.db_type.lower() == "postgresql" else False,
        google_credentials=google_credentials,
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


# ─── Example Management ───────────────────────────────────────────────────────


class SeedExampleItem(BaseModel):
    question: str = Field(min_length=1)
    sql: str = Field(min_length=1)
    product_connection_id: str | None = None


class SeedExamplesRequest(BaseModel):
    examples: list[SeedExampleItem]


@router.post("/{tenant_id}/examples/seed")
async def seed_tenant_examples(
    tenant_id: str,
    payload: SeedExamplesRequest,
    _: None = Depends(verify_admin_token),
) -> dict[str, Any]:
    seeded = 0
    skipped = 0
    errors: list[str] = []

    for idx, item in enumerate(payload.examples):
        label = f"examples[{idx}]"

        try:
            _validate_generated_sql(item.sql)
        except ValueError as validation_error:
            errors.append(f"{label}: invalid SQL — {validation_error}")
            skipped += 1
            continue

        product_conn_id = None
        if item.product_connection_id:
            try:
                product_conn_id = uuid.UUID(item.product_connection_id)
            except ValueError:
                errors.append(f"{label}: product_connection_id is not a valid UUID")
                skipped += 1
                continue

        try:
            result_id = await store_query_example(
                tenant_id=tenant_id,
                question=item.question,
                sql=item.sql,
                product_connection_id=product_conn_id,
                verified_by="admin",
            )
        except Exception as store_error:
            errors.append(f"{label}: store failed — {store_error}")
            skipped += 1
            continue

        if result_id is None:
            errors.append(f"{label}: store returned None (embedding may have failed)")
            skipped += 1
        else:
            seeded += 1

    return {"seeded": seeded, "skipped": skipped, "errors": errors}


@router.get("/{tenant_id}/examples")
async def list_tenant_examples(
    tenant_id: str,
    limit: int = FastAPIQuery(default=50, ge=1, le=500),
    offset: int = FastAPIQuery(default=0, ge=0),
    _: None = Depends(verify_admin_token),
) -> dict[str, Any]:
    if session_factory is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database not configured.",
        )

    from sqlalchemy import text

    query = text(
        "SELECT id, tenant_id, product_connection_id, question, sql, "
        "success_count, last_used_at, verified_by, created_at "
        "FROM tenant_query_examples "
        "WHERE tenant_id = :tenant_id "
        "ORDER BY success_count DESC, last_used_at DESC "
        "LIMIT :limit OFFSET :offset"
    )
    count_query = text(
        "SELECT COUNT(*) FROM tenant_query_examples WHERE tenant_id = :tenant_id"
    )

    try:
        tenant_uuid = uuid.UUID(tenant_id)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="tenant_id is not a valid UUID.")

    async with session_factory() as session:
        total_result = await session.execute(count_query, {"tenant_id": tenant_uuid})
        total = total_result.scalar_one()

        rows_result = await session.execute(query, {"tenant_id": tenant_uuid, "limit": limit, "offset": offset})
        rows = rows_result.mappings().all()

    examples = [
        {
            "id": str(row["id"]),
            "tenant_id": str(row["tenant_id"]),
            "product_connection_id": str(row["product_connection_id"]) if row["product_connection_id"] else None,
            "question": row["question"],
            "sql": row["sql"],
            "success_count": row["success_count"],
            "last_used_at": row["last_used_at"].isoformat() if row["last_used_at"] else None,
            "verified_by": row["verified_by"],
            "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        }
        for row in rows
    ]

    return {"total": total, "limit": limit, "offset": offset, "examples": examples}


@router.delete("/{tenant_id}/examples/{example_id}")
async def delete_tenant_example(
    tenant_id: str,
    example_id: str,
    _: None = Depends(verify_admin_token),
) -> dict[str, Any]:
    if session_factory is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database not configured.",
        )

    from sqlalchemy import text

    try:
        tenant_uuid = uuid.UUID(tenant_id)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="tenant_id is not a valid UUID.")

    try:
        example_uuid = uuid.UUID(example_id)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="example_id is not a valid UUID.")

    delete_stmt = text(
        "DELETE FROM tenant_query_examples "
        "WHERE id = :id AND tenant_id = :tenant_id "
        "RETURNING id"
    )

    async with session_factory() as session:
        result = await session.execute(delete_stmt, {"id": example_uuid, "tenant_id": tenant_uuid})
        deleted_id = result.scalar_one_or_none()
        await session.commit()

    if deleted_id is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Example not found or does not belong to this tenant.",
        )

    return {"deleted": str(deleted_id)}
