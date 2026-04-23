"""add_tenant_query_hints

Revision ID: c2f6d8a9b301
Revises: 533983454fa0
Create Date: 2026-04-23 12:20:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c2f6d8a9b301"
down_revision: Union[str, Sequence[str], None] = "533983454fa0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE tenant_db_credentials "
        "ADD COLUMN IF NOT EXISTS tenant_query_hints TEXT DEFAULT NULL"
    )


def downgrade() -> None:
    op.drop_column("tenant_db_credentials", "tenant_query_hints")
