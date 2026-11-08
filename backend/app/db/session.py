"""Async SQLAlchemy engine/session factory (Person 1).

- PostgreSQL in deployment, SQLite (WAL) locally; schema stays portable.
- SQLite gets pragmas: WAL journal, busy_timeout, foreign_keys ON.
- ``get_session`` is the FastAPI dependency; commits on success and rolls
  back on error so payment paths never leave half-written transactions.
"""

from __future__ import annotations

import logging
from typing import AsyncIterator

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.db.models import *  # noqa: F401,F403 - imported so metadata is populated
from app.db.models import Base as _ModelsBase

logger = logging.getLogger(__name__)

_make_sessionmaker = async_sessionmaker


def create_database_engine(url: str, echo: bool = False) -> AsyncEngine:
    """Create an async engine with dialect-appropriate tuning."""
    if url.startswith("sqlite"):
        engine = create_async_engine(
            url,
            echo=echo,
            connect_args={"check_same_thread": False, "timeout": 30},
        )
        _register_sqlite_pragmas(engine)
    else:
        engine = create_async_engine(url, echo=echo, pool_pre_ping=True)
    return engine


def _register_sqlite_pragmas(async_engine: AsyncEngine) -> None:
    @event.listens_for(async_engine.sync_engine, "connect")
    def _set_sqlite_pragma(dbapi_connection, _record) -> None:  # pragma: no cover
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


def make_session_factory(engine: AsyncEngine) -> async_sessionmaker:
    """Session factory: expire-on-commit off so BackgroundTasks can read state."""
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def create_all(engine: AsyncEngine) -> None:
    """Create every table from the canonical metadata (dev/test bootstrap)."""
    async with engine.begin() as conn:
        await conn.run_sync(_ModelsBase.metadata.create_all)


async def get_session(
    session_factory: async_sessionmaker,
) -> AsyncIterator[AsyncSession]:
    """Yield a session with commit-on-success, rollback-on-error."""
    async with session_factory() as session:
        try:
            yield session
            await session.commit()
            # Phase 4: publish stream events staged during the request ONLY
            # after the commit succeeded (events reflect persisted state).
            from app.services.events import flush_session_events

            await flush_session_events(session)
        except Exception:
            await session.rollback()
            # Drop staged events: their transaction never landed.
            session.info.pop("pending_events", None)
            raise
