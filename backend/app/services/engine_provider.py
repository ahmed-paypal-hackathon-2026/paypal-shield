"""RiskEngine provider (Person 1).

Decouples the API/services layer from Person 2's implementation under
``app/ai/``: routers call ``get_risk_engine()`` and never import ``app.ai``
directly. Selection comes from ``ENGINE_IMPL``:

- ``app_ai``  -> Person 2's ``app.ai.engine`` singleton (default)
- ``stub``    -> deterministic fallback in ``services.stub_engine``

``fallback_engine`` is always the stub and is used when the real engine
raises (REQ-NF-03 graceful degradation).
"""

from __future__ import annotations

import threading
from typing import Protocol, runtime_checkable

from app.config import Settings, get_settings
from app.services.stub_engine import StubRiskEngine

_fallback = StubRiskEngine()


@runtime_checkable
class RiskEngineProtocol(Protocol):
    """Structural mirror of app.schemas.contracts.RiskEngine (read-only)."""


def _load_app_ai_engine():
    # Only entry point into app/ai per Person 2's boundary note.
    from app.ai import engine as app_ai_engine

    return app_ai_engine.engine


class EngineProvider:
    """Lazy, thread-safe engine holder with env-driven selection."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._engine = None
        self._lock = threading.Lock()

    def get(self):
        """Return the configured engine; fall back to the stub on import failure."""
        if self._engine is None:
            with self._lock:
                if self._engine is None:
                    impl = self._settings.engine_impl
                    if impl == "app_ai":
                        try:
                            self._engine = _load_app_ai_engine()
                        except Exception as exc:  # noqa: BLE001 - fail open, never 500
                            self._engine = _fallback
                            # Async logging without secret leak: engine failures are operator-visible.
                            import logging

                            logging.getLogger(__name__).error(
                                "ENGINE_IMPL=app_ai failed to import (%s: %s); "
                                "falling back to deterministic stub engine",
                                type(exc).__name__,
                                exc,
                            )
                    else:
                        self._engine = _fallback
        return self._engine

    @property
    def using_fallback(self) -> bool:
        return self._engine is _fallback


_provider = EngineProvider()


def get_risk_engine():
    """Primary engine used by routers."""
    return _provider.get()


def fallback_engine() -> StubRiskEngine:
    """Deterministic engine for graceful degradation paths (never raises)."""
    return _fallback
