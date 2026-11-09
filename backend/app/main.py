"""FastAPI core (Person 1).

Wires together settings (env-only), the sandbox guard, log redaction, the
database engine/session factory, the risk engine provider and the PayPal
token manager into one modular monolith. Routers live in ``app/api``.

Sandbox guard (spec 7.3): refuses to start against any non-Sandbox PayPal
base URL unless ``ALLOW_NON_SANDBOX=true`` is set explicitly.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import errors, health, quotes
from app.api.deps import get_settings  # noqa: F401 (re-export for convenience)
from app.config import (
    ConfigurationError,
    Settings,
    apply_demo_mode_bridge,
    configure_logging,
    get_settings,
    settings_container,
)
from app.db.session import create_all, create_database_engine, make_session_factory
from app.services.engine_provider import EngineProvider, fallback_engine
from app.services.paypal.auth import TokenManager

logger = logging.getLogger(__name__)

APP_TITLE = "PayPal Shield"
DESCRIPTION = (
    "Real-time risk evaluation and Buyer Protection Fee layer for PayPal Checkout. "
    "Fees are platform service fees under a Protection Plan."
)


def _align_openapi_with_contract(app: FastAPI) -> None:
    """Make the LIVE app's OpenAPI match the frozen contract on our routes.

    The frozen shared/openapi.json is the source of truth; FastAPI synthesizes
    a few pieces of metadata the contract does not contain (anonymous wrapper
    titles for list responses, an empty default media type on the SSE route).
    This strips exactly those so contract-consumers compare byte-for-byte on
    schema content; deliberately ADDITIVE deviations (query params, tags,
    summaries) are documented in docs/phase4-handoff instead, not hidden.
    """

    def openapi() -> dict:
        if app.openapi_schema:
            return app.openapi_schema

        from fastapi.openapi.utils import get_openapi

        schema = get_openapi(
            title=app.title,
            version=app.version,
            description=app.description,
            routes=app.routes,
        )
        try:
            paths = schema.get("paths", {})
            for path in (
                "/api/v1/ledger/transactions",
                "/api/v1/ledger/merchants",
            ):
                content_schema = (
                    paths[path]["get"]["responses"]["200"]["content"][
                        "application/json"
                    ]["schema"]
                )
                content_schema.pop("title", None)
            paths["/api/v1/stream"]["get"]["responses"]["200"]["content"].pop(
                "application/json", None
            )
            components = schema.get("components", {}).get("schemas", {})
            for comp_name in (
                "TransactionRow",
                "MerchantTrustRow",
                "LedgerSummary",
            ):
                component = components.get(comp_name)
                if not component:
                    continue
                component.pop("title", None)
                for field_schema in component.get("properties", {}).values():
                    field_schema.pop("title", None)
                    # pydantic emits numeric bounds as floats (0.0); the frozen
                    # contract writes them as ints (0)
                    if "minimum" in field_schema and field_schema["minimum"] == 0:
                        field_schema["minimum"] = 0
        except KeyError:  # pragma: no cover - route renamed/removed upstream
            logger.warning("openapi alignment skipped: route shape changed")

        app.openapi_schema = schema
        return schema

    app.openapi = openapi  # type: ignore[method-assign]


def create_app(settings: Settings | None = None) -> FastAPI:
    if settings is None:
        settings = get_settings()
        settings_container.cache_clear()
        settings_container()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # ---- startup -----------------------------------------------------
        settings.enforce_sandbox()
        if not settings.hmac_keys():
            raise ConfigurationError(
                "No quote signing key configured. Set QUOTES_HMAC_KEY in your "
                ".env/environment (generate: python -c \"import secrets; "
                "print(secrets.token_urlsafe(48))\"). Refusing to start: every "
                "Buyer Protection Fee quote must be signed."
            )
        apply_demo_mode_bridge(settings)
        if not settings.is_sandbox and settings.allow_non_sandbox:
            logger.warning(
                "Running against a NON-Sandbox PayPal base URL by explicit override: %s",
                settings.paypal_api_base,
            )

        app.state.settings = settings
        app.state.db_engine = create_database_engine(settings.effective_database_url)
        app.state.session_factory = make_session_factory(app.state.db_engine)
        if settings.is_sqlite:
            # Dev/test bootstrap; Postgres deployments run Alembic migrations.
            await create_all(app.state.db_engine)
        if settings.demo_deterministic_mode:
            from app.db.session import seed_demo_merchants

            await seed_demo_merchants(app.state.db_engine)

        app.state.risk_engine = EngineProvider(settings).get()
        app.state.fallback_engine = fallback_engine()
        app.state.token_manager = TokenManager(settings)
        # PayPal REST transport. Priority:
        #   real credentials -> sandbox transport;
        #   no credentials + demo mode -> deterministic no-network stand-in;
        #   otherwise None -> routers degrade to 503 PAYPAL_NOT_CONFIGURED.
        if settings.has_paypal_credentials:
            from app.services.paypal.transport import PayPalTransport

            app.state.transport = PayPalTransport(settings, app.state.token_manager)
        elif settings.demo_deterministic_mode:
            from app.services.paypal.demo_transport import DemoPayPalTransport

            app.state.transport = DemoPayPalTransport()
            logger.info(
                "DEMO MODE: PayPal transport is a deterministic no-network "
                "stand-in (set PAYPAL_CLIENT_ID/SECRET for live Sandbox)"
            )
        else:
            app.state.transport = None

        logger.info(
            "PayPal Shield started (base=%s, sandbox=%s, demo=%s, engine=%s)",
            settings.paypal_api_base,
            settings.is_sandbox,
            settings.demo_deterministic_mode,
            "stub" if isinstance(app.state.risk_engine, type(app.state.fallback_engine)) else "app_ai",
        )
        try:
            yield
        finally:
            # ---- shutdown ----------------------------------------------------
            await app.state.token_manager.aclose()
            await app.state.db_engine.dispose()
            logger.info("PayPal Shield stopped")

    app = FastAPI(
        title=APP_TITLE,
        description=DESCRIPTION,
        version="1.0.0",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.allowed_origin_list,
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    _align_openapi_with_contract(app)

    errors.install_error_handlers(app)

    prefix = settings.api_v1_prefix
    app.include_router(health.router, prefix=prefix, tags=["health"])
    app.include_router(quotes.router, prefix=prefix, tags=["quotes"])
    from app.api import orders as api_orders

    app.include_router(api_orders.router, prefix=prefix, tags=["orders"])

    from app.api import claims as api_claims
    from app.api import webhooks as api_webhooks

    app.include_router(api_claims.router, prefix=prefix, tags=["claims"])
    app.include_router(api_webhooks.router, prefix=prefix, tags=["webhooks"])

    from app.api import ledger as api_ledger
    from app.api import stream as api_stream

    app.include_router(api_ledger.router, prefix=prefix, tags=["ledger"])
    app.include_router(api_stream.router, prefix=prefix, tags=["stream"])

    return app


app = create_app()
