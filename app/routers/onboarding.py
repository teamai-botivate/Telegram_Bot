"""Tenant-facing onboarding endpoints.

These are mounted at /api/onboard. Authentication is JWT-only — passed in the
?token=... query string (GET) or in the request body (POST). No Bearer / x-admin-token.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import select, text

from ..auth.onboarding_jwt import InvalidOnboardingTokenError, verify_token
from ..database import (
    encrypt_credential_value,
    fetch_google_sheet_data,
    fetch_postgres_schema,
    session_factory,
)
from ..models import OnboardingToken, RegisteredClient, Tenant
from ..utils.db_tester import test_postgres_connection

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/onboard", tags=["onboarding"])


# ─── Helpers ────────────────────────────────────────────────────────────────

async def _load_token_row(jti: str) -> OnboardingToken | None:
    if session_factory is None:
        return None
    async with session_factory() as session:
        stmt = select(OnboardingToken).where(OnboardingToken.jwt_jti == jti)
        result = await session.execute(stmt)
        return result.scalar_one_or_none()


async def _load_registered_client(client_id: uuid.UUID | str) -> RegisteredClient | None:
    if session_factory is None:
        return None
    client_uuid = uuid.UUID(str(client_id))
    async with session_factory() as session:
        return await session.get(RegisteredClient, client_uuid)


def _err(status_code: int, code: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"error": code})


def _err_with_message(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"error": code, "message": message})


def _filter_products(
    purchased: list[dict[str, Any]],
    purpose: str,
    product_slug_claim: str | None,
) -> list[dict[str, Any]]:
    """Return the products array shape expected by the frontend."""
    cleaned = []
    for raw in purchased or []:
        if not isinstance(raw, dict):
            continue
        slug = raw.get("slug")
        if not slug:
            continue
        cleaned.append(
            {
                "slug": slug,
                "display_name": raw.get("display_name") or slug,
                "db_type": (raw.get("db_type") or "postgresql").lower(),
            }
        )

    if purpose == "add_database" and product_slug_claim:
        narrowed = [p for p in cleaned if p["slug"] == product_slug_claim]
        if narrowed:
            return narrowed
    return cleaned


# ─── GET /api/onboard/context ───────────────────────────────────────────────

@router.get("/context")
async def get_context(token: str = Query(...)) -> JSONResponse:
    """Verify token and return form-rendering context."""
    # We verify the JWT signature/expiry first, then layer on used-at + revocation
    # checks ourselves so we can return distinct error codes.
    try:
        # verify_token() also checks DB row presence + used_at. We re-derive used vs
        # expired distinction by separately loading the token row before calling it.
        from ..auth.onboarding_jwt import _get_secret  # noqa: PLC2701
        import jwt as _jwt

        try:
            unverified = _jwt.decode(token, _get_secret(), algorithms=["HS256"])
        except _jwt.ExpiredSignatureError:
            return _err(401, "invalid_or_expired_token")
        except _jwt.InvalidTokenError:
            return _err(401, "invalid_or_expired_token")

        jti = unverified.get("jti")
        if not jti:
            return _err(401, "invalid_or_expired_token")

        token_row = await _load_token_row(jti)
        if token_row is None:
            return _err(401, "invalid_or_expired_token")
        if token_row.used_at is not None:
            return _err(409, "token_already_used")

        # Final unified check (expiry vs DB record vs used_at) via the standard helper.
        await verify_token(token)
    except InvalidOnboardingTokenError as exc:
        message = str(exc).lower()
        if "already" in message:
            return _err(409, "token_already_used")
        return _err(401, "invalid_or_expired_token")
    except Exception:
        logger.exception("[ONBOARD] Unexpected error during context lookup.")
        return _err(401, "invalid_or_expired_token")

    client = await _load_registered_client(token_row.registered_client_id)
    if client is None:
        return _err(401, "invalid_or_expired_token")

    expires_at = token_row.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    expires_in_seconds = max(0, int((expires_at - datetime.now(timezone.utc)).total_seconds()))

    products = _filter_products(
        client.purchased_products or [],
        token_row.purpose,
        token_row.product_slug,
    )

    return JSONResponse(
        {
            "company_name": client.company_name,
            "contact_name": client.contact_name,
            "purpose": token_row.purpose,
            "expires_in_seconds": expires_in_seconds,
            "products": products,
        }
    )


# ─── POST /api/onboard/submit ───────────────────────────────────────────────


class OnboardSubmitRequest(BaseModel):
    token: str
    product_slug: str
    db_type: str
    connection_url: str | None = None
    sheet_id: str | None = None
    google_credentials: str | None = None


@router.post("/submit")
async def submit(payload: OnboardSubmitRequest) -> JSONResponse:
    if session_factory is None:
        return _err_with_message(500, "server_misconfigured", "Database is not configured.")

    # ── 1. Verify JWT (and idempotency check) ──────────────────────────────
    try:
        claims = await verify_token(payload.token)
    except InvalidOnboardingTokenError as exc:
        message = str(exc).lower()
        if "already" in message:
            return _err(409, "token_already_used")
        return _err(401, "invalid_or_expired_token")
    except Exception:
        logger.exception("[ONBOARD] Unexpected error verifying submit token.")
        return _err(401, "invalid_or_expired_token")

    jti = claims["jti"]
    registered_client_id = uuid.UUID(claims["sub"])

    # ── 2. Validate body fields against db_type ────────────────────────────
    db_type = (payload.db_type or "").strip().lower()
    if db_type not in ("postgresql", "google_sheets"):
        return _err_with_message(400, "invalid_db_type", f"Unsupported db_type: {payload.db_type!r}.")

    if db_type == "postgresql":
        if not payload.connection_url:
            return _err_with_message(400, "missing_field", "connection_url is required for PostgreSQL.")
    else:
        if not payload.sheet_id:
            return _err_with_message(400, "missing_field", "sheet_id is required for Google Sheets.")
        if not payload.google_credentials:
            return _err_with_message(400, "missing_field", "google_credentials is required for Google Sheets.")
        try:
            json.loads(payload.google_credentials)
        except (ValueError, TypeError):
            return _err_with_message(400, "invalid_credentials_json", "google_credentials must be a valid JSON string.")

    # ── 3. Confirm product_slug is in the client's purchased products ──────
    client = await _load_registered_client(registered_client_id)
    if client is None:
        return _err(401, "invalid_or_expired_token")

    purchased = client.purchased_products or []
    matched_product = next(
        (p for p in purchased if isinstance(p, dict) and p.get("slug") == payload.product_slug),
        None,
    )
    if matched_product is None:
        return _err_with_message(
            400,
            "product_not_purchased",
            f"Product {payload.product_slug!r} is not part of this client's purchased products.",
        )

    # The product's db_type from purchased_products is authoritative — don't trust the body's db_type
    # if it disagrees, since the badge in the form is read-only and derived from this same source.
    expected_db_type = (matched_product.get("db_type") or "postgresql").lower()
    if expected_db_type != db_type:
        return _err_with_message(
            400,
            "db_type_mismatch",
            f"Product {payload.product_slug!r} expects db_type={expected_db_type!r}, got {db_type!r}.",
        )

    product_display_name = matched_product.get("display_name") or payload.product_slug

    # ── 4. Test the Postgres connection (skip for Sheets) ──────────────────
    if db_type == "postgresql":
        ok, friendly_error = await test_postgres_connection(
            payload.connection_url, ssl_required=True, timeout=10.0
        )
        if not ok:
            logger.info(
                "[ONBOARD] Postgres connection test failed for client=%s product=%s: %s",
                registered_client_id, payload.product_slug, friendly_error,
            )
            return _err_with_message(400, "connection_failed", friendly_error)

    # ── 5. Schema introspection (now non-blocking — uses AsyncOpenAI + to_thread) ─
    schema_blueprint: str | None = None
    auto_schema_hints: str | None = None

    if db_type == "postgresql":
        try:
            schema_blueprint, auto_schema_hints = await fetch_postgres_schema(payload.connection_url)
        except Exception as exc:
            logger.exception("[ONBOARD] Postgres schema introspection failed.")
            return _err_with_message(400, "schema_introspection_failed", str(exc))
    else:
        try:
            schema_blueprint, auto_schema_hints = await fetch_google_sheet_data(
                payload.sheet_id, payload.google_credentials
            )
        except Exception as exc:
            logger.exception("[ONBOARD] Google Sheets schema introspection failed.")
            return _err_with_message(400, "schema_introspection_failed", str(exc))

    # ── 6. Encrypt credentials ─────────────────────────────────────────────
    if db_type == "postgresql":
        encrypted_connection_url = encrypt_credential_value(payload.connection_url)
        encrypted_google_credentials = None
        stored_connection_url = encrypted_connection_url
    else:
        encrypted_google_credentials = encrypt_credential_value(payload.google_credentials)
        # Store the sheet identifier in the same column (matches existing convention).
        stored_connection_url = encrypt_credential_value(f"google_sheets://{payload.sheet_id}")

    # ── 7. Single-transaction write ────────────────────────────────────────
    purpose = claims.get("purpose", "initial_setup")

    async with session_factory() as session:
        try:
            # Re-check used_at inside the transaction so a race between two submits
            # with the same jti yields exactly one success.
            row = await session.execute(
                text(
                    "SELECT used_at FROM onboarding_tokens WHERE jwt_jti = :jti FOR UPDATE"
                ),
                {"jti": jti},
            )
            token_status = row.first()
            if token_status is None:
                await session.rollback()
                return _err(401, "invalid_or_expired_token")
            if token_status.used_at is not None:
                await session.rollback()
                return _err(409, "token_already_used")

            # Resolve / create the tenant row.
            tenant_id: uuid.UUID
            client_row = await session.get(RegisteredClient, registered_client_id)
            if client_row is None:
                await session.rollback()
                return _err(401, "invalid_or_expired_token")

            if client_row.tenant_id is None:
                # Initial setup — create tenant and link.
                purchased_slugs = [
                    p["slug"]
                    for p in (client_row.purchased_products or [])
                    if isinstance(p, dict) and p.get("slug")
                ]
                tenant = Tenant(
                    company_name=client_row.company_name,
                    active_modules=purchased_slugs or [payload.product_slug],
                    telegram_chat_id=client_row.telegram_chat_id,
                    whatsapp_number=client_row.whatsapp_number,
                )
                session.add(tenant)
                await session.flush()
                tenant_id = tenant.id

                client_row.tenant_id = tenant_id
            else:
                tenant_id = client_row.tenant_id

                # Reject if a credential row already exists for this tenant + product.
                existing = await session.execute(
                    text(
                        "SELECT 1 FROM tenant_db_credentials "
                        "WHERE tenant_id = :tid AND product_slug = :slug LIMIT 1"
                    ),
                    {"tid": tenant_id, "slug": payload.product_slug},
                )
                if existing.first() is not None:
                    await session.rollback()
                    return _err_with_message(
                        409,
                        "product_already_connected",
                        f"A database is already connected for {product_display_name}.",
                    )

            # Insert tenant_db_credentials. (This row IS the product↔DB connection — see
            # note at the bottom of this file. There is no separate product_connections table.)
            display_name = f"{client_row.company_name} {product_display_name}".strip()
            await session.execute(
                text(
                    "INSERT INTO tenant_db_credentials "
                    "(id, tenant_id, db_type, product_slug, display_name, connection_url, "
                    " google_credentials, schema_blueprint, auto_schema_hints, ssl_required) "
                    "VALUES (:id, :tenant_id, :db_type, :product_slug, :display_name, :connection_url, "
                    "        :google_credentials, :schema_blueprint, :auto_schema_hints, :ssl_required)"
                ),
                {
                    "id": uuid.uuid4(),
                    "tenant_id": tenant_id,
                    "db_type": db_type,
                    "product_slug": payload.product_slug,
                    "display_name": display_name,
                    "connection_url": stored_connection_url,
                    "google_credentials": encrypted_google_credentials,
                    "schema_blueprint": schema_blueprint,
                    "auto_schema_hints": auto_schema_hints,
                    "ssl_required": db_type == "postgresql",
                },
            )

            # Mark the token as used.
            await session.execute(
                text("UPDATE onboarding_tokens SET used_at = NOW() WHERE jwt_jti = :jti"),
                {"jti": jti},
            )

            await session.commit()
        except Exception:
            await session.rollback()
            logger.exception("[ONBOARD] Submit transaction failed.")
            return _err_with_message(500, "submit_failed", "Could not save credentials. Please try again.")

    logger.info(
        "[ONBOARD] Credentials accepted client=%s product=%s db_type=%s purpose=%s",
        registered_client_id, payload.product_slug, db_type, purpose,
    )

    return JSONResponse({"success": True})


# ─── Note on `product_connections` ───────────────────────────────────────────
#
# The original spec mentions inserting a row into a `product_connections` table.
# That table does not exist in the schema (Prompt 1 only added `registered_clients`
# and `onboarding_tokens`, plus `product_slug` + `display_name` columns on
# `tenant_db_credentials`). After auditing the codebase, the existing
# `tenant_db_credentials` row is the natural product↔database join: it now carries
# `tenant_id`, `product_slug`, `display_name`, `db_type`, and the encrypted
# credentials. Adding a separate one-to-one mirror table would be redundant.
# Flagged in the response summary.
