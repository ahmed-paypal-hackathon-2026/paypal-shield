"""Ledger split + reputation counters (Person 1, spec sections 2.7 / 4.1).

``post_capture_entries`` runs INSIDE the capture transaction:
- two ``ledger_entries`` rows: MERCHANT_REVENUE (item price) and, when the
  buyer opted in, PROTECTION_FEE (the Buyer Protection Fee);
- one atomic reputation ``UPDATE`` per counter
      SET total_orders = total_orders + 1, successful_orders = successful_orders + 1
  Never a read-modify-write loop: concurrent captures cannot lose updates.

Rows are idempotent per order: a second call for the same order inserts
nothing (checked inside the same transaction, and capture replay never
reaches this module anyway because it short-circuits on state CAPTURED).
"""

from __future__ import annotations

import datetime as _dt
import logging
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy import update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.response_models import MerchantTrustRow, TransactionRow
from app.db.models import (
    LedgerEntryRow,
    MerchantReputationRow,
    OrderRow,
    QuoteRow,
)

logger = logging.getLogger(__name__)

KIND_MERCHANT_REVENUE = "MERCHANT_REVENUE"
KIND_PROTECTION_FEE = "PROTECTION_FEE"
KIND_REFUND = "REFUND"

# Frozen-contract record fields are shared shapes ("mirrors" until Person 4
# promotes them into schemas/contracts.py), so the builders below live beside
# the write-side ledger code they read, and services importing response shapes
# is intentional: response_models only depends on schemas.contracts.


async def post_capture_entries(session: AsyncSession, order: OrderRow) -> None:
    """Insert the capture ledger split and bump reputation counters atomically.

    Must be called within the capture transaction; ``session.flush()`` is left
    to the caller. Safe to call twice for one order (second call is a no-op).
    """
    if hasattr(order, "internal_order_id"):
        existing = (
            await session.execute(
                select(LedgerEntryRow.id)
                .where(LedgerEntryRow.internal_order_id == order.internal_order_id)
                .limit(1)
            )
        ).scalar_one_or_none()
        if existing is not None:
            logger.info(
                "ledger rows for order %s already exist; skipping duplicate post",
                order.internal_order_id,
            )
            return

    rows = [
        LedgerEntryRow(
            internal_order_id=order.internal_order_id,
            kind=KIND_MERCHANT_REVENUE,
            amount=Decimal(str(order.item_price)),
            currency_code=order.currency_code,
        )
    ]
    fee = Decimal(str(order.protection_fee))
    if order.opted_in and fee > 0:
        rows.append(
            LedgerEntryRow(
                internal_order_id=order.internal_order_id,
                kind=KIND_PROTECTION_FEE,
                amount=fee,
                currency_code=order.currency_code,
            )
        )
    session.add_all(rows)

    # Atomic counter bump: single UPDATE with SQL-side expressions, never
    # a read-modify-write loop (concurrent captures cannot lose updates).
    await session.execute(
        sa_update(MerchantReputationRow)
        .where(MerchantReputationRow.merchant_id == order.merchant_id)
        .values(
            total_orders=MerchantReputationRow.total_orders + 1,
            successful_orders=MerchantReputationRow.successful_orders + 1,
        )
    )

    logger.info(
        "posted capture ledger for order %s (merchant=%s, fee=%s)",
        order.internal_order_id, order.merchant_id, fee,
    )


__all__ = [
    "post_capture_entries",
    "KIND_MERCHANT_REVENUE",
    "KIND_PROTECTION_FEE",
    "transaction_row_for_order",
    "merchant_trust_row_for",
    "merchant_trust_row_by_id",
]


# ---------------------------------------------------------------------------
# Read-only record builders (Ledger API + Stream events). NO writes here.
# ---------------------------------------------------------------------------
def aware_utc(dt: _dt.datetime) -> _dt.datetime:
    """Contract timestamps are timezone-aware UTC datetimes."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=_dt.timezone.utc)
    return dt


_aware = aware_utc  # internal alias


async def transaction_row_for_order(
    session: AsyncSession,
    order: OrderRow,
) -> TransactionRow:
    """Build the frozen-contract ``TransactionRow`` for an order.

    One extra PK lookup joins the quote's risk score ``r_final`` — the only
    contract field not stored on ``orders``. Called with the already-loaded
    order row post-commit, so the record always reflects persisted state.
    """
    quote = await session.get(QuoteRow, order.quote_id)
    r_final = Decimal(str(quote.r_final)) if quote is not None else Decimal("0.00")
    return TransactionRow(
        internal_order_id=str(order.internal_order_id),
        merchant_id=order.merchant_id,
        item_price=Decimal(str(order.item_price)),
        protection_fee=Decimal(str(order.protection_fee)),
        r_final=r_final,
        state=order.state,
        created_at=_aware(order.created_at),
    )


def _derive_merchant_trust(rep: MerchantReputationRow) -> tuple[Decimal, Decimal]:
    """Derive Bayesian trust and fraud penalty multiplier without importing app.ai."""
    prior_m = Decimal(10)
    prior_c = Decimal("0.70")
    trust = (Decimal(rep.successful_orders) + prior_m * prior_c) / (
        Decimal(rep.total_orders) + prior_m
    )
    penalty = (
        Decimal("1.00")
        if rep.verified_fraud_claims == 0
        else Decimal("1.50")
        if rep.verified_fraud_claims == 1
        else Decimal("2.00")
    )
    return trust.quantize(Decimal("0.0001")), penalty.quantize(Decimal("0.01"))


def merchant_trust_row_for(row: MerchantReputationRow) -> MerchantTrustRow:
    """Project a reputation DB row through the Bayesian scorer.

    Formula (spec section 6.1 / 6.2): trust =
    (successful_orders + 7) / (total_orders + 10), m_penalty
    1.00 / 1.50 / 2.00 for 0 / 1 / 2+ verified fraud claims. Trust is derived
    on read — never stored, never invented.
    """
    trust, m_penalty = _derive_merchant_trust(row)
    return MerchantTrustRow(
        merchant_id=row.merchant_id,
        total_orders=int(row.total_orders),
        successful_orders=int(row.successful_orders),
        verified_fraud_claims=int(row.verified_fraud_claims),
        trust_score=trust,
        m_penalty=m_penalty,
    )


async def merchant_trust_row_by_id(
    session: AsyncSession,
    merchant_id: str,
) -> Optional[MerchantTrustRow]:
    """Post-commit factory for stream events: trust row by merchant id."""
    row = await session.get(MerchantReputationRow, merchant_id)
    return merchant_trust_row_for(row) if row is not None else None
