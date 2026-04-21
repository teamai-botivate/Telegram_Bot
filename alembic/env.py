from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from dotenv import load_dotenv
from sqlalchemy import engine_from_config, pool
from sqlalchemy.engine import make_url

from app.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

load_dotenv()


def _get_migration_database_url() -> tuple[str, dict]:
    """Return a (url, connect_args) pair compatible with psycopg2 (used by Alembic)."""
    database_url = os.getenv("DATABASE_URL", "").strip()
    if not database_url:
        raise RuntimeError("DATABASE_URL is not configured. Set it in your .env file.")

    parsed = make_url(database_url)

    # Strip asyncpg-only query params
    query = dict(parsed.query)
    ssl_val = query.pop("ssl", None)

    connect_args = {}
    if ssl_val == "require" or query.pop("sslmode", None) == "require":
        connect_args["sslmode"] = "require"

    # Force psycopg2 driver (sync, used by Alembic)
    parsed = parsed.set(drivername="postgresql+psycopg2", query=query)
    return parsed.render_as_string(hide_password=False), connect_args


_migration_url, _connect_args = _get_migration_database_url()
config.set_main_option("sqlalchemy.url", _migration_url)
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        connect_args=_connect_args,
    )

    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
