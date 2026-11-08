"""Order endpoints (Person 1, spec section 3.3 step 2-3).

POST /api/v1/orders            -> create the PayPal order for a signed quote
POST /api/v1/orders/{id}/capture -> capture; id = PayPal order id

Thin routers only: verification, state machines and reconciliation land in
services.orders_flow; nothing PayPal-specific lives here.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, get_settings
from app.config import Settings
from app.schemas.contracts import CaptureResponse, CreateOrderRequest
from app.schemas.contracts import CreateOrderResponse
from app.services import orders_flow

logger = logging.getLogger(__name__)

router = APIRouter()


def _get_transport(request: Request):
    transport = getattr(request.app.state, "transport", None)
    if transport is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="PayPal is not configured (missing sandbox credentials)",
        )
    return transport


@router.post(
    "/orders",
    response_model=CreateOrderResponse,
    status_code=status.HTTP_201_CREATED,
    operation_id="createOrder",
)
async def create_order(
    body: CreateOrderRequest,
    request: Request,
    session: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> CreateOrderResponse:
    transport = _get_transport(request)
    try:
        return await orders_flow.create_order(
            session,
            transport,
            settings,
            quote_id=body.quote_id,
            buyer_ref=body.buyer_ref,
            opt_in=body.opt_in,
        )
    except HTTPException:
        raise


@router.post("/orders/{id}/capture", response_model=CaptureResponse)
async def capture_order(
    id: str,
    request: Request,
    session: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> CaptureResponse:
    transport = _get_transport(request)
    try:
        return await orders_flow.capture_order(
            session,
            transport,
            settings,
            paypal_order_id=id,
        )
    except HTTPException:
        raise
