"""add_auto_schema_hints_drop_tenant_query_hints

Revision ID: df2d0f4c6d3a
Revises: c2f6d8a9b301
Create Date: 2026-04-23
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "df2d0f4c6d3a"
down_revision = "c2f6d8a9b301"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE tenant_db_credentials "
        "ADD COLUMN IF NOT EXISTS auto_schema_hints TEXT DEFAULT NULL"
    )
    op.execute(
        "ALTER TABLE tenant_db_credentials "
        "DROP COLUMN IF EXISTS tenant_query_hints"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE tenant_db_credentials "
        "ADD COLUMN IF NOT EXISTS tenant_query_hints TEXT DEFAULT NULL"
    )
    op.execute(
        "ALTER TABLE tenant_db_credentials "
        "DROP COLUMN IF EXISTS auto_schema_hints"
    )

