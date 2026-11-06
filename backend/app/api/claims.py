"""Claim endpoints (Person 1, spec sections 3.4 / 6.6).

POST /api/v1/claims            -> submit (multipart: order_id/order_ref + photo)
GET  /api/v1/claims/{id}       -> read a claim as ClaimRecord
POST /api/v1/claims/{id}/resolve -> reviewer decision (X-Reviewer-Token auth)

Thin routers only: eligibility, the state machine, the arbiter and refunds
live in services.claims. Submission schedules the heavy pipeline (evidence,
engine arbitration, potential PayPal refund call) in a FastAPI BackgroundTask
so the HTTP response returns fast; the task opens its OWN session because the
dependency session is committed + closed by the time the task runs.

Field names match the frozen frontend client: ``order_id`` + ``photo``
(``order_ref`` is accepted as a compatibility alias); resolve body
``{decision, reason_code}``.
"""

from __future__ import annotations

import hmac
import logging
from decimal import Decimal, InvalidOperation
from typing import Annotated, Literal, Optional

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Form,
    Header,
    HTTPException,
    Request,
    Response,
    UploadFile,
    status,
)
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, get_session_factory, get_settings
from app.config import Settings
from app.db.models import ClaimRow
from app.schemas.contracts import ClaimRecord
from app.services import claims as claims_service
from app.services.claims import ClaimAmountError

logger = logging.getLogger(__name__)

router = APIRouter()


class ResolveClaimRequest(BaseModel):
    """Frozen resolve body: decision is required; reason_code is optional."""

    decision: Literal["REFUND", "REJECT"]
    reason_code: str = ""


def _get_transport(request: Request):
    transport = getattr(request.app.state, "transport", None)
    if transport is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="PayPal is not configured (missing sandbox credentials)",
        )
    return transport


def _require_reviewer_token(settings: Settings, header: Optional[str]) -> None:
    """Constant-time token check; wrong/missing token -> 401."""
    expected = settings.claim_reviewer_token
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="CLAIM_REVIEWER_TOKEN not configured; reviewer actions disabled",
        )
    supplied = header or ""
    if not hmac.compare_digest(supplied, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid reviewer token",
        )


@router.post("/claims", response_model=ClaimRecord, status_code=status.HTTP_201_CREATED)
async def submit_claim(
    background_tasks: BackgroundTasks,
    request: Request,
    response: Response,
    photo: Annotated[UploadFile, File(...)],
    order_id: Annotated[Optional[str], Form()] = None,
    order_ref: Annotated[Optional[str], Form()] = None,
    claim_amount: Annotated[Optional[str], Form()] = None,
    session: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> ClaimRecord:
    """Submit a counterfeit claim for a captured order.

    Fast (201) response with the claim in SUBMITTED state; evidence +
    arbitration run in a background task and the record is refreshed by GET.
    """
    transport = _get_transport(request)
    session_factory = get_session_factory(request)
    order_reference = order_id or order_ref
    if not order_reference:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="order_id or order_ref is required",
        )

    # Read at most one byte beyond the configured limit so validation can
    # reject oversized files without loading an unbounded upload into memory.
    photo_bytes = await photo.read(settings.claim_photo_max_bytes + 1)
    amount: Optional[Decimal] = None
    if claim_amount:
        try:
            amount = Decimal(claim_amount.strip())
        except (InvalidOperation, ValueError):
            raise ClaimAmountError("claim_amount must be a decimal like '60.00'") from None

    # Preserve the API's idempotency signal: a first submission is 201;
    # replaying the order's existing claim is 200 with the same ClaimRecord.
    existing = None
    order = await claims_service.find_order_flexible(session, order_reference)
    if order is not None:
        existing = (
            await session.execute(
                select(ClaimRow).where(
                    ClaimRow.internal_order_id == order.internal_order_id
                )
            )
        ).scalar_one_or_none()

    claim = await claims_service.submit_claim(
        session, settings,
        order_ref=order_reference,
        photo_bytes=photo_bytes,
        claim_amount=amount,
    )
    if existing is not None:
        response.status_code = status.HTTP_200_OK

    # Commit NOW (and publish the staged claim/add stream event) so the feed
    # shows the submission before the background pipeline's updates: Starlette
    # runs BackgroundTasks before the get_session dependency teardown, so a
    # teardown-only commit would let claim/update overtake claim/add.
    from app.services.events import commit_and_emit

    await commit_and_emit(session)

    background_tasks.add_task(
        claims_service.run_claim_pipeline,
        session_factory,
        settings,
        transport,
        claim_id=str(claim.claim_id),
    )
    return await claims_service.get_claim_record(session, str(claim.claim_id))


@router.get("/claims/{id}", response_model=ClaimRecord)
async def get_claim(
    id: str,
    session: AsyncSession = Depends(get_db),
) -> ClaimRecord:
    return await claims_service.get_claim_record(session, id)


@router.post("/claims/{id}/resolve", response_model=ClaimRecord)
async def resolve_claim(
    id: str,
    request: Request,
    body: ResolveClaimRequest,
    x_reviewer_token: Annotated[Optional[str], Header()] = None,
    session: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> ClaimRecord:
    """Reviewer decision on a MANUAL_REVIEW claim (spec 3.4 last row)."""
    _require_reviewer_token(settings, x_reviewer_token)
    transport = _get_transport(request)
    session_factory = get_session_factory(request)

    await claims_service.resolve_claim(
        session, session_factory, settings, transport,
        claim_id=id,
        decision=body.decision,
        reason_code=body.reason_code,
        reviewer="reviewer",
    )
    return await claims_service.get_claim_record(session, id)
