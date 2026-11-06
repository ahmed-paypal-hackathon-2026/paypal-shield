"""Ledger read endpoints (Person 1, frozen contract /api/v1/ledger/*).

Three contract GETs — all strictly read-only over the same ledger rows that
``post_capture_entries`` / the refund closed loop write. A GET never inserts,
updates or deletes a financial row; balances are computed from persisted
entries only (never from client-provided values).

CONTRACT DEVIATIONS (documented, additive only — the frozen contract declares
no parameters, so defaults keep it byte-compatible):
- ``limit``/``offset`` pagination (default 100, max 200) + deterministic
  ``created_at DESC`` ordering with ``internal_order_id`` tie-break, because an
  unordered, unpaginated feed is unusable for the live-ops board the frontend
  mocks model.
- ``merchant_id``/``state`` optional equality filters (validated enums where a
  closed set exists).
- No per-entry / per-order / per-claim detail endpoint exists in the frozen
  contract, so none is registered (the ledger rows are per-order by design:
  ``GET /claims/{id}`` already reads a claim and its order linkage).

Security note: the frozen contract declares no securitySchemes and these are
aggregated financial-statistics endpoints (no buyer-identifiable fields);
contract-faithful caching-protection is via no-store headers.
"""

from __future__ import annotations

import logging
from decimal import Decimal, ROUND_HALF_UP
from typing import Literal, Optional

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.api.response_models import LedgerSummary, MerchantTrustRow, TransactionRow
from app.db.models import (
    LedgerEntryRow,
    MerchantReputationRow,
    OrderRow,
    QuoteRow,
)
from app.services.ledger import (
    KIND_MERCHANT_REVENUE,
    KIND_PROTECTION_FEE,
    KIND_REFUND,
    aware_utc,
    merchant_trust_row_for,
)

logger = logging.getLogger(__name__)

router = APIRouter()


OrderState = Literal[  # type: ignore[valid-type]
    "QUOTED",
    "ORDER_CREATED",
    "CAPTURED",
    "CAPTURE_FAILED",
    "PENDING_RECONCILIATION",
    "PARTIALLY_REFUNDED",
    "REFUNDED",
]


@router.get(
    "/ledger/transactions",
    operation_id="listTransactions",
    responses={
        200: {
            "description": "Order transaction rows",
            "model": list[TransactionRow],
        },
    },
)
async def list_transactions(
    response: Response,
    session: AsyncSession = Depends(get_db),
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    merchant_id: Optional[str] = Query(default=None, min_length=1, max_length=64),
    state: Optional[OrderState] = Query(default=None),
) -> list[TransactionRow]:
    """Order transaction rows, most recent first.

    One aggregate query (orders joined to their quote for ``r_final``) —
    no per-row queries, so the list stays N+1-free at any limit.
    """
    filters = []
    if merchant_id:
        filters.append(OrderRow.merchant_id == merchant_id)
    if state:
        filters.append(OrderRow.state == state)

    stmt = (
        select(OrderRow, QuoteRow.r_final)
        .join(QuoteRow, OrderRow.quote_id == QuoteRow.quote_id)
        .order_by(OrderRow.created_at.desc(), OrderRow.internal_order_id.desc())
        .offset(offset)
        .limit(limit)
    )
    if filters:
        stmt = stmt.where(*filters)
    rows = (await session.execute(stmt)).all()

    response.headers["Cache-Control"] = "no-store"
    return [
        TransactionRow(
            internal_order_id=str(order.internal_order_id),
            merchant_id=order.merchant_id,
            item_price=Decimal(str(order.item_price)),
            protection_fee=Decimal(str(order.protection_fee)),
            r_final=Decimal(str(r_final)),
            state=order.state,
            created_at=aware_utc(order.created_at),
        )
        for order, r_final in rows
    ]


@router.get(
    "/ledger/merchants",
    operation_id="listMerchants",
    responses={
        200: {
            "description": "Merchant trust registry rows",
            "model": list[MerchantTrustRow],
        },
    },
)
async def list_merchants(
    response: Response,
    session: AsyncSession = Depends(get_db),
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> list[MerchantTrustRow]:
    """Merchant trust registry — trust derived on read via the Phase 2 oracle.

    Single query over reputation rows; the Bayesian projection happens in
    Python (score_reputation) so the formula lives in exactly one place.
    """
    response.headers["Cache-Control"] = "no-store"
    rows = (
        await session.execute(
            select(MerchantReputationRow)
            .order_by(MerchantReputationRow.merchant_id.asc())
            .offset(offset)
            .limit(limit)
        )
    ).scalars().all()
    return [merchant_trust_row_for(rep) for rep in rows]


@router.get(
    "/ledger/summary",
    operation_id="ledgerSummary",
    response_model=LedgerSummary,
    responses={
        200: {"description": "Network-level totals shown in the pinned grid row"},
    },
)
async def ledger_summary(
    response: Response,
    session: AsyncSession = Depends(get_db),
) -> LedgerSummary:
    """Network totals for the pinned grid row.

    The three money totals are summed EXACTLY: amounts load as Decimal and are
    aggregated in Python. SQLite stores MoneyAmount as a canonical string and
    SQL ``SUM`` over it would coerce through binary float, which the money
    rules of this project forbid. One table scan of (kind, amount) only.
    """
    response.headers["Cache-Control"] = "no-store"
    rows = (
        await session.execute(select(LedgerEntryRow.kind, LedgerEntryRow.amount))
    ).all()

    total_volume = Decimal("0")
    protection_fees = Decimal("0")
    refunds_issued = Decimal("0")
    for kind, amount in rows:
        value = Decimal(str(amount))
        if kind == KIND_MERCHANT_REVENUE:
            total_volume += value
        elif kind == KIND_PROTECTION_FEE:
            protection_fees += value
        elif kind == KIND_REFUND:
            refunds_issued += value

    if total_volume > 0:
        net_loss_ratio = (refunds_issued / total_volume).quantize(
            Decimal("0.0001"), rounding=ROUND_HALF_UP
        )
    else:
        net_loss_ratio = Decimal("0.0000")

    return LedgerSummary(
        total_volume=total_volume,
        protection_fees_collected=protection_fees,
        refunds_issued=refunds_issued,
        net_loss_ratio=net_loss_ratio,
    )
