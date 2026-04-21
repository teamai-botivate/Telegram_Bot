"""multi-tenant schema

Revision ID: 20260421_multi_tenant_schema
Revises:
Create Date: 2026-04-21 00:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = "20260421_multi_tenant_schema"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("DROP TABLE IF EXISTS purchases CASCADE")
    op.execute("DROP TABLE IF EXISTS products CASCADE")
    op.execute("DROP TABLE IF EXISTS customers CASCADE")

    op.create_table(
        "tenants",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("company_name", sa.String(length=255), nullable=False),
            op.execute("DROP TABLE IF EXISTS orders CASCADE")
            op.execute("DROP TABLE IF EXISTS client_customers CASCADE")
            op.execute("DROP TABLE IF EXISTS meeting_attendees CASCADE")
            op.execute("DROP TABLE IF EXISTS meeting_tasks CASCADE")
            op.execute("DROP TABLE IF EXISTS meetings CASCADE")
            op.execute("DROP TABLE IF EXISTS botivate_users CASCADE")
            op.execute("DROP TABLE IF EXISTS purchases CASCADE")
            op.execute("DROP TABLE IF EXISTS products CASCADE")
            op.execute("DROP TABLE IF EXISTS customers CASCADE")
            op.execute("DROP TABLE IF EXISTS tenant_schema_map CASCADE")
            op.execute("DROP TABLE IF EXISTS tenant_db_credentials CASCADE")
            op.execute("DROP TABLE IF EXISTS tenants CASCADE")
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("telegram_chat_id"),
        sa.UniqueConstraint("whatsapp_number"),
    )
                sa.Column("telegram_chat_id", sa.String(length=50), nullable=True),
    op.create_index("ix_tenants_telegram_chat_id", "tenants", ["telegram_chat_id"], unique=True)
    op.create_index("ix_tenants_whatsapp_number", "tenants", ["whatsapp_number"], unique=True)

    op.create_table(
                sa.UniqueConstraint("telegram_chat_id"),
        "botivate_users",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
            op.create_index("ix_tenants_telegram_chat_id", "tenants", ["telegram_chat_id"], unique=True)
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("phone", sa.String(length=20), nullable=False),
                "tenant_db_credentials",
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("id"),
                sa.Column("db_type", sa.String(length=32), nullable=False),
                sa.Column("host", sa.Text(), nullable=False),
                sa.Column("port", sa.Text(), nullable=False),
                sa.Column("database_name", sa.Text(), nullable=False),
                sa.Column("db_user", sa.Text(), nullable=False),
                sa.Column("db_password", sa.Text(), nullable=False),
                sa.Column("schema_map", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
                sa.Column("ssl_required", sa.Boolean(), server_default=sa.text("true"), nullable=False),
                sa.Column("last_connected_at", sa.DateTime(timezone=True), nullable=True),
    op.create_table(
        "meetings",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
            op.create_index("ix_tenant_db_credentials_tenant_id", "tenant_db_credentials", ["tenant_id"], unique=True)
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=False),
                "tenant_schema_map",
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
                sa.Column("module", sa.String(length=64), nullable=False),
                sa.Column("intent", sa.String(length=64), nullable=False),
                sa.Column("sql_template", sa.Text(), nullable=False),
    op.create_table(
        "meeting_tasks",
                sa.UniqueConstraint("tenant_id", "module", "intent", name="uq_tenant_module_intent"),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
            op.create_index("ix_tenant_schema_map_tenant_id", "tenant_schema_map", ["tenant_id"], unique=False)
        sa.Column("meeting_id", postgresql.UUID(as_uuid=True), nullable=False),

    op.drop_index("ix_client_customers_tenant_id", table_name="client_customers")
            op.drop_index("ix_tenant_schema_map_tenant_id", table_name="tenant_schema_map")
            op.drop_table("tenant_schema_map")

            op.drop_index("ix_tenant_db_credentials_tenant_id", table_name="tenant_db_credentials")
            op.drop_table("tenant_db_credentials")
    op.create_table(
            op.drop_index("ix_tenants_telegram_chat_id", table_name="tenants")
        "customers",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
    op.create_index("ix_meeting_attendees_tenant_id", "meeting_attendees", ["tenant_id"], unique=False)
