"""Periodic sync from the Botivate Main DB (Supabase) into NeonDB's registered_clients allowlist.

The Main DB has a flat `clients` table with one row per (client, product) pair. This job
groups rows by contact identifier (WhatsApp number / Telegram chat id) and upserts a single
`registered_clients` record with `purchased_products` as a JSONB array.

Operates read-only against the Main DB and never crashes the bot — every operation is
guarded so the scheduler can keep ticking on partial failure.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any

import asyncpg
from dotenv import load_dotenv
from sqlalchemy import select, text

from ..database import session_factory
from ..models import RegisteredClient

load_dotenv()

logger = logging.getLogger(__name__)

BOTIVATE_MAIN_DB_URL = os.getenv("BOTIVATE_MAIN_DB_URL", "")
BOTIVATE_MAIN_DB_SYNC_INTERVAL_MINUTES = int(
    os.getenv("BOTIVATE_MAIN_DB_SYNC_INTERVAL_MINUTES", "15")
)
BOTIVATE_MAIN_DB_QUERY = os.getenv(
    "BOTIVATE_MAIN_DB_QUERY",
    "SELECT * FROM clients WHERE is_active = TRUE",
)
BOTIVATE_MAIN_DB_CONNECT_TIMEOUT_SECONDS = float(
    os.getenv("BOTIVATE_MAIN_DB_CONNECT_TIMEOUT_SECONDS", "30")
)


_PRODUCT_DISPLAY_NAMES = {
    "task_delegation": "Task Delegation & Performance Scoring",
    "checklist": "Checklist",
    "minutes_of_meeting": "Minutes of Meeting",
}


def _normalize_phone(raw: str | None) -> str | None:
    """Strip spaces, dashes, parens; ensure leading +. Returns None for empty input."""
    if raw is None:
        return None
    cleaned = re.sub(r"[\s\-()]", "", str(raw)).strip()
    if not cleaned:
        return None
    if cleaned.startswith("+"):
        return "+" + re.sub(r"\D", "", cleaned[1:])
    digits = re.sub(r"\D", "", cleaned)
    if not digits:
        return None
    return "+" + digits


def _normalize_chat_id(raw: Any) -> str | None:
    if raw is None:
        return None
    cleaned = str(raw).strip()
    return cleaned or None


def _product_display_name(slug: str, fallback: str | None = None) -> str:
    return _PRODUCT_DISPLAY_NAMES.get(slug) or (fallback or slug.replace("_", " ").title())


async def _fetch_main_db_rows() -> list[dict[str, Any]]:
    if not BOTIVATE_MAIN_DB_URL:
        raise RuntimeError("BOTIVATE_MAIN_DB_URL is not configured.")

    # Read-only: we open a connection, run a SELECT, close it. Never write.
    connection = await asyncpg.connect(
        BOTIVATE_MAIN_DB_URL,
        timeout=BOTIVATE_MAIN_DB_CONNECT_TIMEOUT_SECONDS,
    )
    try:
        rows = await connection.fetch(BOTIVATE_MAIN_DB_QUERY)
        return [dict(row) for row in rows]
    finally:
        await connection.close()


def _group_clients(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group multiple product rows per client into one record per unique contact."""
    grouped: dict[tuple[str | None, str | None], dict[str, Any]] = {}

    for row in rows:
        whatsapp = _normalize_phone(row.get("whatsapp_number"))
        telegram = _normalize_chat_id(row.get("telegram_chat_id"))
        phone = _normalize_phone(row.get("phone_number"))

        if whatsapp is None and telegram is None:
            # No contact handle to dedupe on — skip; bot can't reach this client.
            continue

        key = (whatsapp, telegram)
        slug = (row.get("product_slug") or "").strip()
        db_type = (row.get("db_type") or "postgresql").strip().lower()
        product_entry = (
            {
                "slug": slug,
                "db_type": db_type,
                "display_name": _product_display_name(slug),
            }
            if slug
            else None
        )

        if key not in grouped:
            grouped[key] = {
                "external_id": str(row.get("id")) if row.get("id") is not None else None,
                "company_name": (row.get("company_name") or "").strip() or "Unknown",
                "contact_name": (row.get("contact_name") or "").strip() or "Unknown",
                "phone_number": phone,
                "whatsapp_number": whatsapp,
                "telegram_chat_id": telegram,
                "email": (row.get("email") or None) or None,
                "purchased_products": [],
                "is_active": bool(row.get("is_active", True)),
            }

        record = grouped[key]
        # Preserve any non-empty contact info encountered across rows.
        if not record["email"] and row.get("email"):
            record["email"] = row["email"]
        if not record["phone_number"] and phone:
            record["phone_number"] = phone

        if product_entry and not any(p.get("slug") == slug for p in record["purchased_products"]):
            record["purchased_products"].append(product_entry)

    return list(grouped.values())


async def _upsert_registered_client(record: dict[str, Any]) -> str:
    """Returns 'created' or 'updated'. Raises on hard failure."""
    if session_factory is None:
        raise RuntimeError("DATABASE_URL is not configured for NeonDB.")

    purchased_products_json = json.dumps(record["purchased_products"])
    whatsapp = record["whatsapp_number"]
    telegram = record["telegram_chat_id"]

    async with session_factory() as session:
        # Find any existing row matching either WA or Telegram. We can't rely on
        # ON CONFLICT because both keys are nullable.
        find_clauses = []
        params: dict[str, Any] = {}
        if whatsapp is not None:
            find_clauses.append("whatsapp_number = :whatsapp")
            params["whatsapp"] = whatsapp
        if telegram is not None:
            find_clauses.append("telegram_chat_id = :telegram")
            params["telegram"] = telegram

        find_stmt = text(
            "SELECT id, tenant_id FROM registered_clients "
            f"WHERE {' OR '.join(find_clauses)} "
            "ORDER BY synced_at DESC LIMIT 1"
        )
        result = await session.execute(find_stmt, params)
        existing = result.first()

        if existing is not None:
            update_stmt = text(
                "UPDATE registered_clients SET "
                "  external_id = COALESCE(:external_id, external_id), "
                "  company_name = :company_name, "
                "  contact_name = :contact_name, "
                "  phone_number = COALESCE(:phone_number, phone_number), "
                "  whatsapp_number = COALESCE(:whatsapp_number, whatsapp_number), "
                "  telegram_chat_id = COALESCE(:telegram_chat_id, telegram_chat_id), "
                "  email = COALESCE(:email, email), "
                "  purchased_products = CAST(:purchased_products AS jsonb), "
                "  is_active = :is_active, "
                "  synced_at = NOW() "
                "WHERE id = :id"
            )
            await session.execute(
                update_stmt,
                {
                    "id": existing.id,
                    "external_id": record["external_id"],
                    "company_name": record["company_name"],
                    "contact_name": record["contact_name"],
                    "phone_number": record["phone_number"],
                    "whatsapp_number": record["whatsapp_number"],
                    "telegram_chat_id": record["telegram_chat_id"],
                    "email": record["email"],
                    "purchased_products": purchased_products_json,
                    "is_active": record["is_active"],
                },
            )
            await session.commit()
            return "updated"

        insert_stmt = text(
            "INSERT INTO registered_clients "
            "(external_id, company_name, contact_name, phone_number, whatsapp_number, "
            " telegram_chat_id, email, purchased_products, is_active, synced_at) "
            "VALUES (:external_id, :company_name, :contact_name, :phone_number, :whatsapp_number, "
            "        :telegram_chat_id, :email, CAST(:purchased_products AS jsonb), :is_active, NOW())"
        )
        await session.execute(
            insert_stmt,
            {
                "external_id": record["external_id"],
                "company_name": record["company_name"],
                "contact_name": record["contact_name"],
                "phone_number": record["phone_number"],
                "whatsapp_number": record["whatsapp_number"],
                "telegram_chat_id": record["telegram_chat_id"],
                "email": record["email"],
                "purchased_products": purchased_products_json,
                "is_active": record["is_active"],
            },
        )
        await session.commit()
        return "created"


async def _deactivate_missing_clients(seen_keys: set[tuple[str | None, str | None]]) -> int:
    """Mark registered_clients as is_active=FALSE if their contact handles are no longer
    in the Main DB allowlist. Returns count of newly deactivated rows."""
    if session_factory is None or not seen_keys:
        return 0

    async with session_factory() as session:
        statement = select(RegisteredClient).where(RegisteredClient.is_active.is_(True))
        result = await session.execute(statement)
        existing_clients = result.scalars().all()

        deactivated = 0
        for client in existing_clients:
            key = (client.whatsapp_number, client.telegram_chat_id)
            if key in seen_keys:
                continue
            # Don't deactivate alternates (e.g. only WA matched, telegram differs) — only
            # deactivate when neither handle appears in any synced record.
            if any(
                client.whatsapp_number is not None and k[0] == client.whatsapp_number
                for k in seen_keys
            ):
                continue
            if any(
                client.telegram_chat_id is not None and k[1] == client.telegram_chat_id
                for k in seen_keys
            ):
                continue

            client.is_active = False
            deactivated += 1

        if deactivated:
            await session.commit()
        return deactivated


async def sync_registered_clients() -> dict[str, Any]:
    """Pull from Botivate Main DB and reconcile NeonDB's registered_clients table."""
    summary: dict[str, Any] = {
        "synced": 0,
        "created": 0,
        "updated": 0,
        "deactivated": 0,
        "errors": [],
    }

    try:
        rows = await _fetch_main_db_rows()
    except Exception as exc:
        logger.exception("[SYNC] Failed to read from Botivate Main DB.")
        summary["errors"].append(f"main_db_read: {exc}")
        return summary

    grouped = _group_clients(rows)
    summary["synced"] = len(grouped)
    seen_keys: set[tuple[str | None, str | None]] = set()

    for record in grouped:
        try:
            outcome = await _upsert_registered_client(record)
            if outcome == "created":
                summary["created"] += 1
            elif outcome == "updated":
                summary["updated"] += 1
            seen_keys.add((record["whatsapp_number"], record["telegram_chat_id"]))
        except Exception as exc:
            logger.exception(
                "[SYNC] Failed to upsert registered_client whatsapp=%s telegram=%s",
                record.get("whatsapp_number"),
                record.get("telegram_chat_id"),
            )
            summary["errors"].append(
                f"upsert {record.get('whatsapp_number') or record.get('telegram_chat_id')}: {exc}"
            )

    try:
        summary["deactivated"] = await _deactivate_missing_clients(seen_keys)
    except Exception as exc:
        logger.exception("[SYNC] Failed during deactivation sweep.")
        summary["errors"].append(f"deactivate: {exc}")

    logger.info(
        "[SYNC] created=%d updated=%d deactivated=%d errors=%d",
        summary["created"],
        summary["updated"],
        summary["deactivated"],
        len(summary["errors"]),
    )
    return summary


# ── Scheduler ─────────────────────────────────────────────────────────────────

_scheduler = None


async def start_sync_scheduler() -> None:
    """Start the recurring background sync job. Safe to call once at app startup."""
    global _scheduler

    if not BOTIVATE_MAIN_DB_URL:
        logger.info("[SYNC] BOTIVATE_MAIN_DB_URL not configured; sync scheduler disabled.")
        return

    if _scheduler is not None:
        logger.info("[SYNC] Scheduler already running.")
        return

    try:
        from apscheduler.schedulers.asyncio import AsyncIOScheduler
    except ImportError:
        logger.warning("[SYNC] apscheduler is not installed; sync scheduler disabled.")
        return

    scheduler = AsyncIOScheduler(timezone="UTC")

    async def _tick() -> None:
        try:
            await sync_registered_clients()
        except Exception:
            logger.exception("[SYNC] Unhandled error during scheduled sync tick.")

    interval = max(1, BOTIVATE_MAIN_DB_SYNC_INTERVAL_MINUTES)
    scheduler.add_job(
        _tick,
        "interval",
        minutes=interval,
        next_run_time=datetime.now(timezone.utc),
        id="botivate_main_db_sync",
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()
    _scheduler = scheduler
    logger.info("[SYNC] Scheduler started; interval=%d min.", interval)


async def stop_sync_scheduler() -> None:
    global _scheduler
    if _scheduler is None:
        return
    try:
        _scheduler.shutdown(wait=False)
    except Exception:
        logger.exception("[SYNC] Error shutting down scheduler.")
    _scheduler = None
