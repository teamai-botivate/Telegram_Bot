"""JWT issuance and verification for the self-service onboarding flow.

Tokens are single-use: once a form submission marks used_at, verify_token() rejects
any further presentation of the same jti. Expiry is 30 minutes by default.

Env vars consumed:
  ONBOARDING_JWT_SECRET   — signing secret (distinct from ADMIN_SECRET_TOKEN)
  ONBOARDING_BASE_URL     — base URL for the form link, e.g. https://bot.botivate.in
"""

from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import jwt
from dotenv import load_dotenv
from sqlalchemy import select, text

from ..database import session_factory

load_dotenv()

logger = logging.getLogger(__name__)

ONBOARDING_JWT_SECRET = os.getenv("ONBOARDING_JWT_SECRET", "")
ONBOARDING_BASE_URL = os.getenv("ONBOARDING_BASE_URL", "")
_ALGORITHM = "HS256"
_EXPIRY_MINUTES = 30


class InvalidOnboardingTokenError(Exception):
    """Raised when a token is missing, expired, already used, or tampered with."""


def _get_secret() -> str:
    if not ONBOARDING_JWT_SECRET:
        raise RuntimeError("ONBOARDING_JWT_SECRET is not configured. Add it to your .env file.")
    return ONBOARDING_JWT_SECRET


def build_form_url(token: str) -> str:
    base = ONBOARDING_BASE_URL.rstrip("/")
    if not base:
        raise RuntimeError("ONBOARDING_BASE_URL is not configured. Add it to your .env file.")
    return f"{base}/static/onboard.html?token={token}"


async def issue_token(
    registered_client_id: uuid.UUID | str,
    purpose: str,
    product_slug: str | None = None,
) -> tuple[str, str]:
    """Mint a signed JWT and persist an audit row in onboarding_tokens.

    Returns (jwt_string, jti).
    """
    if session_factory is None:
        raise RuntimeError("DATABASE_URL is not configured.")

    secret = _get_secret()
    jti = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(minutes=_EXPIRY_MINUTES)

    claims: dict[str, Any] = {
        "sub": str(registered_client_id),
        "purpose": purpose,
        "jti": jti,
        "iat": int(now.timestamp()),
        "exp": int(expires_at.timestamp()),
    }
    if product_slug is not None:
        claims["product_slug"] = product_slug

    token = jwt.encode(claims, secret, algorithm=_ALGORITHM)

    async with session_factory() as session:
        stmt = text(
            "INSERT INTO onboarding_tokens "
            "(registered_client_id, purpose, product_slug, jwt_jti, expires_at) "
            "VALUES (:registered_client_id, :purpose, :product_slug, :jwt_jti, :expires_at)"
        )
        await session.execute(
            stmt,
            {
                "registered_client_id": uuid.UUID(str(registered_client_id)),
                "purpose": purpose,
                "product_slug": product_slug,
                "jwt_jti": jti,
                "expires_at": expires_at,
            },
        )
        await session.commit()

    return token, jti


async def verify_token(token: str) -> dict[str, Any]:
    """Decode and validate a token. Returns the claims dict on success.

    Raises InvalidOnboardingTokenError for any failure:
      - bad signature / malformed
      - expired (JWT exp)
      - already used (used_at IS NOT NULL in DB)
      - revoked / not found in DB
    """
    if session_factory is None:
        raise RuntimeError("DATABASE_URL is not configured.")

    secret = _get_secret()

    try:
        claims = jwt.decode(token, secret, algorithms=[_ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise InvalidOnboardingTokenError("Onboarding link has expired. Please request a new one.")
    except jwt.InvalidTokenError as exc:
        raise InvalidOnboardingTokenError(f"Invalid onboarding token: {exc}")

    jti = claims.get("jti")
    if not jti:
        raise InvalidOnboardingTokenError("Token is missing jti claim.")

    async with session_factory() as session:
        row_stmt = text(
            "SELECT used_at, expires_at FROM onboarding_tokens WHERE jwt_jti = :jti LIMIT 1"
        )
        result = await session.execute(row_stmt, {"jti": jti})
        row = result.first()

    if row is None:
        raise InvalidOnboardingTokenError("Onboarding token not recognised.")

    if row.used_at is not None:
        raise InvalidOnboardingTokenError("Onboarding link has already been used.")

    now = datetime.now(timezone.utc)
    expires_at = row.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if now > expires_at:
        raise InvalidOnboardingTokenError("Onboarding link has expired. Please request a new one.")

    return claims


async def mark_token_used(jti: str) -> None:
    """Set used_at on the token row after a successful form submission."""
    if session_factory is None:
        return

    async with session_factory() as session:
        await session.execute(
            text("UPDATE onboarding_tokens SET used_at = NOW() WHERE jwt_jti = :jti"),
            {"jti": jti},
        )
        await session.commit()
