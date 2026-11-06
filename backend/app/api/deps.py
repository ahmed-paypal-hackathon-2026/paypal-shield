"""Router dependencies (Person 1).

State is attached to ``app.state`` by ``app.main.create_app``:
- ``settings``        config.Settings
- ``risk_engine``     the configured RiskEngine
- ``fallback_engine`` deterministic stub (graceful degradation)
- ``db_engine``       AsyncEngine
- ``session_factory`` async_sessionmaker
- ``token_manager``   paypal.auth.TokenManager
"""

from __future__ import annotations

from typing import AsyncIterator

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, settings_container
from app.db.session import get_session as _yield_session
from app.services.engine_provider import fallback_engine as _fallback_engine
from app.services.engine_provider import get_risk_engine as _get_risk_engine


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_session_factory(request: Request):
    return request.app.state.session_factory


def get_risk_engine(request: Request):
    return request.app.state.risk_engine


def get_fallback_engine(request: Request):
    return request.app.state.fallback_engine


def get_token_manager(request: Request):
    return request.app.state.token_manager


async def get_db(
    factory=Depends(get_session_factory),
) -> AsyncIterator[AsyncSession]:
    """Commit-on-success / rollback-on-error request session."""
    async for session in _yield_session(factory):
        yield session


__all__ = [
    "get_settings",
    "get_session_factory",
    "get_db",
    "get_risk_engine",
    "get_fallback_engine",
    "get_token_manager",
    "settings_container",
    "_get_risk_engine",
    "_fallback_engine",
]
