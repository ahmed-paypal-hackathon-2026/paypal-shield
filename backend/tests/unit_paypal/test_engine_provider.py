"""Risk-engine provider boundary tests (Person 1)."""

from __future__ import annotations

from app.ai import engine as app_ai_engine
from app.config import Settings
from app.services.engine_provider import EngineProvider


def test_app_ai_provider_returns_public_engine_singleton():
    """ENGINE_IMPL=app_ai must not silently degrade because of adapter drift."""
    provider = EngineProvider(Settings(_env_file=None, engine_impl="app_ai"))

    assert provider.get() is app_ai_engine
    assert provider.using_fallback is False
