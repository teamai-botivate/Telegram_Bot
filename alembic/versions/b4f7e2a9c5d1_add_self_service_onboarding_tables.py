"""add_self_service_onboarding_tables

Revision ID: b4f7e2a9c5d1
Revises: e7a1c4b8d2f5
Create Date: 2026-05-03

"""
from typing import Sequence, Union

from alembic import op


revision: str = "b4f7e2a9c5d1"
down_revision: Union[str, Sequence[str], None] = "e7a1c4b8d2f5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS registered_clients (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            external_id TEXT NULL,
            company_name TEXT NOT NULL,
            contact_name TEXT NOT NULL,
            phone_number TEXT NULL,
            whatsapp_number TEXT NULL,
            telegram_chat_id TEXT NULL,
            email TEXT NULL,
            purchased_products JSONB NOT NULL DEFAULT '[]'::jsonb,
            is_active BOOLEAN NOT NULL DEFAULT TRUE,
            synced_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            tenant_id UUID NULL REFERENCES tenants(id) ON DELETE SET NULL
        )
        """
    )

    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_registered_clients_whatsapp_number "
        "ON registered_clients (whatsapp_number) "
        "WHERE whatsapp_number IS NOT NULL"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_registered_clients_telegram_chat_id "
        "ON registered_clients (telegram_chat_id) "
        "WHERE telegram_chat_id IS NOT NULL"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_registered_clients_phone_number "
        "ON registered_clients (phone_number) "
        "WHERE phone_number IS NOT NULL"
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS onboarding_tokens (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            registered_client_id UUID NOT NULL REFERENCES registered_clients(id) ON DELETE CASCADE,
            purpose TEXT NOT NULL CHECK (purpose IN ('initial_setup', 'add_database')),
            product_slug TEXT NULL,
            jwt_jti TEXT NOT NULL UNIQUE,
            expires_at TIMESTAMPTZ NOT NULL,
            used_at TIMESTAMPTZ NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )

    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_onboarding_tokens_jwt_jti "
        "ON onboarding_tokens (jwt_jti)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_onboarding_tokens_registered_client_id "
        "ON onboarding_tokens (registered_client_id)"
    )

    op.execute(
        "ALTER TABLE tenant_db_credentials "
        "ADD COLUMN IF NOT EXISTS product_slug TEXT NULL"
    )
    op.execute(
        "ALTER TABLE tenant_db_credentials "
        "ADD COLUMN IF NOT EXISTS display_name TEXT NULL"
    )
    op.execute(
        "ALTER TABLE tenant_db_credentials "
        "ALTER COLUMN db_type SET DEFAULT 'postgresql'"
    )

    # A tenant must be allowed to own multiple credential rows (one per product DB).
    # Drop any UNIQUE constraint or unique index on tenant_id alone if it exists.
    op.execute(
        """
        DO $$
        DECLARE
            cons_name TEXT;
            idx_name TEXT;
        BEGIN
            FOR cons_name IN
                SELECT conname
                FROM pg_constraint
                WHERE conrelid = 'tenant_db_credentials'::regclass
                  AND contype = 'u'
                  AND conkey = ARRAY[
                      (SELECT attnum FROM pg_attribute
                       WHERE attrelid = 'tenant_db_credentials'::regclass
                         AND attname = 'tenant_id')
                  ]
            LOOP
                EXECUTE 'ALTER TABLE tenant_db_credentials DROP CONSTRAINT ' || quote_ident(cons_name);
            END LOOP;

            FOR idx_name IN
                SELECT i.relname
                FROM pg_index x
                JOIN pg_class i ON i.oid = x.indexrelid
                JOIN pg_class t ON t.oid = x.indrelid
                WHERE t.relname = 'tenant_db_credentials'
                  AND x.indisunique
                  AND NOT x.indisprimary
                  AND x.indnatts = 1
                  AND (SELECT attname FROM pg_attribute
                       WHERE attrelid = t.oid
                         AND attnum = x.indkey[0]) = 'tenant_id'
            LOOP
                EXECUTE 'DROP INDEX IF EXISTS ' || quote_ident(idx_name);
            END LOOP;
        END$$;
        """
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE tenant_db_credentials "
        "ALTER COLUMN db_type DROP DEFAULT"
    )
    op.execute(
        "ALTER TABLE tenant_db_credentials "
        "DROP COLUMN IF EXISTS display_name"
    )
    op.execute(
        "ALTER TABLE tenant_db_credentials "
        "DROP COLUMN IF EXISTS product_slug"
    )

    op.execute("DROP INDEX IF EXISTS ix_onboarding_tokens_registered_client_id")
    op.execute("DROP INDEX IF EXISTS ix_onboarding_tokens_jwt_jti")
    op.execute("DROP TABLE IF EXISTS onboarding_tokens")

    op.execute("DROP INDEX IF EXISTS ix_registered_clients_phone_number")
    op.execute("DROP INDEX IF EXISTS ix_registered_clients_telegram_chat_id")
    op.execute("DROP INDEX IF EXISTS ix_registered_clients_whatsapp_number")
    op.execute("DROP TABLE IF EXISTS registered_clients")
