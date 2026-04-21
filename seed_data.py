from __future__ import annotations

import asyncio
from typing import Any

from sqlalchemy import or_, select

from app.database import (
    create_tables,
    encrypt_credential_value,
    save_schema_map,
    save_tenant_credentials,
    session_factory,
)
from app.models import Tenant


SAMPLE_WHATSAPP_NUMBER = "+91XXXXXXXXXX"
SAMPLE_TELEGRAM_CHAT_ID = "123456789"

FAKE_POSTGRES_CREDENTIALS: dict[str, Any] = {
    "db_type": "postgresql",
    "host": "127.0.0.1",
    "port": 5432,
    "database_name": "tenant_demo_db",
    "db_user": "tenant_demo_user",
    "db_password": "tenant_demo_password",
    "ssl_required": False,
}

DEMO_SCHEMA_MAPS = [
    {
        "module": "minutes_of_meeting",
        "intent": "meeting_schedule",
        "sql_template": (
            "SELECT meeting_title, scheduled_at, location, status "
            "FROM tbl_meetings "
            "WHERE scheduled_at >= NOW() "
            "ORDER BY scheduled_at ASC "
            "LIMIT 20"
        ),
    },
    {
        "module": "minutes_of_meeting",
        "intent": "task_status",
        "sql_template": (
            "SELECT task_name, status, due_date "
            "FROM tbl_tasks "
            "WHERE assigned_employee ILIKE $1 "
            "ORDER BY due_date ASC NULLS LAST "
            "LIMIT 20"
        ),
    },
    {
        "module": "delivery_tracker",
        "intent": "delivery_status",
        "sql_template": (
            "SELECT order_number, status, expected_delivery_date "
            "FROM tbl_orders "
            "WHERE customer_name ILIKE $1 "
            "ORDER BY updated_at DESC "
            "LIMIT 20"
        ),
    },
]


async def seed() -> None:
    await create_tables()

    if session_factory is None:
        raise RuntimeError("Database session factory is not configured. Set DATABASE_URL in .env.")

    async with session_factory() as session:
        existing_tenant = await session.scalar(
            select(Tenant).where(
                or_(
                    Tenant.whatsapp_number == SAMPLE_WHATSAPP_NUMBER,
                    Tenant.telegram_chat_id == SAMPLE_TELEGRAM_CHAT_ID,
                )
            )
        )
        if existing_tenant is not None:
            print("Seed data already exists for the sample tenant. Skipping.")
            return

        tenant = Tenant(
            company_name="Demo Corp",
            telegram_chat_id=SAMPLE_TELEGRAM_CHAT_ID,
            whatsapp_number=SAMPLE_WHATSAPP_NUMBER,
            active_modules=["minutes_of_meeting", "delivery_tracker"],
        )
        session.add(tenant)
        await session.commit()
        await session.refresh(tenant)

    encrypted_fields = {
        "host": encrypt_credential_value(FAKE_POSTGRES_CREDENTIALS["host"]),
        "port": encrypt_credential_value(str(FAKE_POSTGRES_CREDENTIALS["port"])),
        "database_name": encrypt_credential_value(FAKE_POSTGRES_CREDENTIALS["database_name"]),
        "db_user": encrypt_credential_value(FAKE_POSTGRES_CREDENTIALS["db_user"]),
        "db_password": encrypt_credential_value(FAKE_POSTGRES_CREDENTIALS["db_password"]),
    }

    await save_tenant_credentials(
        tenant_id=tenant.id,
        db_type=FAKE_POSTGRES_CREDENTIALS["db_type"],
        encrypted_fields=encrypted_fields,
        ssl_required=FAKE_POSTGRES_CREDENTIALS["ssl_required"],
    )

    for schema_map in DEMO_SCHEMA_MAPS:
        await save_schema_map(
            tenant_id=tenant.id,
            module=schema_map["module"],
            intent=schema_map["intent"],
            sql_template=schema_map["sql_template"],
        )

    print("Meta seed data inserted successfully.")


if __name__ == "__main__":
    asyncio.run(seed())
