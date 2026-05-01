"""add_tenant_query_examples

Revision ID: e7a1c4b8d2f5
Revises: df2d0f4c6d3a
Create Date: 2026-05-01

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "e7a1c4b8d2f5"
down_revision: Union[str, Sequence[str], None] = "df2d0f4c6d3a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.execute(
        """
        CREATE TABLE tenant_query_examples (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            product_connection_id UUID NULL,
            question TEXT NOT NULL,
            sql TEXT NOT NULL,
            question_embedding vector(1536) NOT NULL,
            success_count INTEGER NOT NULL DEFAULT 1,
            last_used_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            verified_by TEXT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )

    op.create_index(
        "ix_tenant_query_examples_tenant_id",
        "tenant_query_examples",
        ["tenant_id"],
        unique=False,
    )
    op.create_index(
        "ix_tenant_query_examples_tenant_product",
        "tenant_query_examples",
        ["tenant_id", "product_connection_id"],
        unique=False,
    )

    op.execute(
        "CREATE INDEX ix_tenant_query_examples_embedding "
        "ON tenant_query_examples "
        "USING ivfflat (question_embedding vector_cosine_ops) "
        "WITH (lists = 100)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_tenant_query_examples_embedding")
    op.drop_index(
        "ix_tenant_query_examples_tenant_product",
        table_name="tenant_query_examples",
    )
    op.drop_index(
        "ix_tenant_query_examples_tenant_id",
        table_name="tenant_query_examples",
    )
    op.drop_table("tenant_query_examples")
