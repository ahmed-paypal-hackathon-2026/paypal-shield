"""GET /api/v1/health (Person 1).

Returns service status, live Sandbox connectivity (real OAuth probe when
credentials are configured) and demo mode. Terminology rule honored: this
service is a Buyer Protection platform using Protection Plan terminology.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request

from app.api.deps import get_token_manager
from app.api.response_models import HealthResponse
from app.services.paypal.auth import TokenManager

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/health", response_model=HealthResponse)
async def health(
    request: Request,
    token_manager: TokenManager = Depends(get_token_manager),
) -> HealthResponse:
    settings = request.app.state.settings
    demo_mode = settings.demo_deterministic_mode

    paypal_sandbox_connected = False
    if token_manager.has_credentials:
        try:
            await token_manager.get_token()
            paypal_sandbox_connected = True
        except Exception as exc:  # noqa: BLE001 - health must not raise
            logger.warning("Sandbox OAuth probe failed: %s: %s", type(exc).__name__, exc)

    # With demo mode or connectivity the service is usable for checkout flows.
    is_ok = demo_mode or paypal_sandbox_connected or not token_manager.has_credentials
    return HealthResponse(
        status="ok" if is_ok else "degraded",
        paypal_sandbox_connected=paypal_sandbox_connected,
        demo_mode=demo_mode,
    )
