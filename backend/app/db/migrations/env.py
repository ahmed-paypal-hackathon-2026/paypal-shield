"""Alembic migration environment for PayPal Shield (Person 1).

The database URL always comes from app.config settings (env-only), never
from alembic.ini. Migration scripts import the canonical metadata from
app.db.models so the initial migration cannot drift from the models.
"""

from __future__ import annotations

import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection

# Make ``app`` importable when alembic runs from backend/
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from app.config import get_settings  # noqa: E402
from app.db.models import Base  # noqa: E402

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def get_url() -> str:
    return get_settings().effective_database_url


def run_migrations_offline() -> None:
    """Emit SQL to stdout without a DB connection."""
    context.configure(
        url=get_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=get_url().startswith("sqlite"),
    )
    with context.begin_transaction():
        context.run_migrations()


def _run_migrations_on_connection(connection: Connection) -> None:
    is_sqlite = connection.dialect.name == "sqlite"
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        # Batch migrations keep SQLite ALTERs working.
        render_as_batch=is_sqlite,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Async-capable environments (aiosqlite / psycopg async)."""
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(get_url(), poolclass=pool.NullPool)
    async with engine.connect() as connection:
        await connection.run_sync(_run_migrations_on_connection)
    await engine.dispose()


def do_run_migrations() -> None:
    if get_url().startswith(("postgresql+asyncpg", "sqlite+aiosqlite", "postgresql+psycopg")):
        import anyio

        anyio.run(run_async_migrations)
    else:
        from sqlalchemy import create_engine

        connectable = create_engine(get_url(), poolclass=pool.NullPool)
        with connectable.connect() as connection:
            _run_migrations_on_connection(connection)
        connectable.dispose()


def run_migrations_online() -> None:
    do_run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
