"""Canonical ORM models for the 9 PayPal Shield tables (Person 1).

One-to-one mirror of the PostgreSQL schema in spec section 5.2, written with
portable types (see ``money.MoneyAmount``, JSONB via variant, TIMESTAMPTZ via
``DateTime(timezone=True)``). All money values are ``Decimal`` — never float.
SQLAlchemy models mirror the schema one-to-one; the reputation row is the
most critical table (Bayesian trust, penalty multiplier, closed loop).
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import List, Optional

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.money import MoneyAmount

# JSONB on PostgreSQL, plain JSON elsewhere (SQLite stores JSON as text).
JSON_VARIANT = JSON().with_variant(JSONB(), "postgresql")

# IMPORTANT (Person 1): every child-table FK gets a declared ``relationship``.
# SQLAlchemy's unit-of-work creates flush-order edges ONLY from relationships —
# a bare ``ForeignKey()`` column does not order inserts (verified empirically:
# alphabetical-class order raised spurious FK violations on SQLite when
# several tables are inserted in ONE flush, which payment transactions do).
# ``lazy="raise"`` keeps accidental lazy loads impossible in async code.
_CHILD_REL = {"lazy": "raise"}


# ---------------------------------------------------------------------------
# merchant_reputation — pillar 5 source of truth; the closed-loop target row
# ---------------------------------------------------------------------------
class MerchantReputationRow(Base):
    __tablename__ = "merchant_reputation"

    merchant_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    total_orders: Mapped[int] = mapped_column(default=0)
    successful_orders: Mapped[int] = mapped_column(default=0)
    verified_fraud_claims: Mapped[int] = mapped_column(default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        CheckConstraint("total_orders >= 0", name="ck_rep_total_orders"),
        CheckConstraint("successful_orders >= 0", name="ck_rep_successful_orders"),
        CheckConstraint("verified_fraud_claims >= 0", name="ck_rep_fraud_nonneg"),
        CheckConstraint("successful_orders <= total_orders", name="ck_rep_success_le_total"),
    )


# ---------------------------------------------------------------------------
# risk_cache — pre-computed pillar scores keyed by image+text hash (with TTL)
# ---------------------------------------------------------------------------
class RiskCacheRow(Base):
    __tablename__ = "risk_cache"

    image_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    text_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    pillars_json: Mapped[dict] = mapped_column(JSON_VARIANT, nullable=False)
    model_versions: Mapped[dict] = mapped_column(JSON_VARIANT, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


# ---------------------------------------------------------------------------
# quotes — HMAC-signed Buyer Protection Fee quotes
# ---------------------------------------------------------------------------
class QuoteRow(Base):
    __tablename__ = "quotes"

    # Ordering-only relationship (child side): guarantees merchant row
    # exists / flushes before the quote without exposing merchant state.
    merchant: Mapped["MerchantReputationRow"] = relationship(lazy="raise")

    quote_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    merchant_id: Mapped[str] = mapped_column(
        ForeignKey("merchant_reputation.merchant_id"), nullable=False
    )
    item_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    item_price: Mapped[Decimal] = mapped_column(MoneyAmount(14, 2), nullable=False)
    currency_code: Mapped[str] = mapped_column(String(3), nullable=False)
    protection_fee: Mapped[Decimal] = mapped_column(MoneyAmount(14, 2), nullable=False)
    r_final: Mapped[Decimal] = mapped_column(MoneyAmount(5, 2), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    hmac_signature: Mapped[str] = mapped_column(Text, nullable=False)
    consumed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


# ---------------------------------------------------------------------------
# orders — external PayPal order linkage + internal state machine state
# States: QUOTED, ORDER_CREATED, CAPTURED, PARTIALLY_REFUNDED, REFUNDED,
#         QUOTE_EXPIRED, CAPTURE_FAILED, PENDING_RECONCILIATION
# ---------------------------------------------------------------------------
class OrderRow(Base):
    __tablename__ = "orders"

    internal_order_id: Mapped[str] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    quote_id: Mapped[str] = mapped_column(ForeignKey("quotes.quote_id"), nullable=False)
    paypal_order_id: Mapped[Optional[str]] = mapped_column(String(64), unique=True)
    paypal_capture_id: Mapped[Optional[str]] = mapped_column(String(64), unique=True)
    merchant_id: Mapped[str] = mapped_column(
        ForeignKey("merchant_reputation.merchant_id"), nullable=False
    )
    buyer_ref: Mapped[str] = mapped_column(String(120), nullable=False)
    opted_in: Mapped[bool] = mapped_column(Boolean, nullable=False)
    item_price: Mapped[Decimal] = mapped_column(MoneyAmount(14, 2), nullable=False)
    protection_fee: Mapped[Decimal] = mapped_column(MoneyAmount(14, 2), nullable=False, default=0)
    currency_code: Mapped[str] = mapped_column(String(3), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    dispute_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    # Ordering-only relationships (declared after the FK columns they reference).
    quote: Mapped["QuoteRow"] = relationship(foreign_keys=[quote_id], lazy="raise")
    merchant: Mapped["MerchantReputationRow"] = relationship(
        foreign_keys=[merchant_id], lazy="raise"
    )

    __table_args__ = (Index("ix_orders_merchant_id", "merchant_id"),)


# ---------------------------------------------------------------------------
# webhook_events — PayPal deliveries, deduplicated by event_id
# ---------------------------------------------------------------------------
class WebhookEventRow(Base):
    __tablename__ = "webhook_events"

    event_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON_VARIANT, nullable=False)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    processed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    __table_args__ = (Index("ix_webhook_events_type", "event_type"),)


# ---------------------------------------------------------------------------
# claims — one counterfeit claim per order (UNIQUE internal_order_id)
# States: SUBMITTED, EVIDENCE_CHECKED, AUTO_APPROVED, MANUAL_REVIEW,
#         REJECTED, REFUNDED
# ---------------------------------------------------------------------------
class ClaimRow(Base):
    __tablename__ = "claims"

    order: Mapped["OrderRow"] = relationship(lazy="raise")

    claim_id: Mapped[str] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    internal_order_id: Mapped[str] = mapped_column(
        ForeignKey("orders.internal_order_id"), nullable=False
    )
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    claim_amount: Mapped[Decimal] = mapped_column(MoneyAmount(14, 2), nullable=False)
    currency_code: Mapped[str] = mapped_column(String(3), nullable=False)
    received_image_hash: Mapped[Optional[str]] = mapped_column(String(64))
    discrepancy: Mapped[Optional[Decimal]] = mapped_column(MoneyAmount(5, 4))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("internal_order_id", name="uq_claims_order"),
        Index("ix_claims_state", "state"),
    )


# SQLite lacks BIGSERIAL; Integer PK gives SQLite autoincrement while
# PostgreSQL keeps BigInteger (BIGSERIAL semantics) via the variant.
_INT_PK = BigInteger().with_variant(Integer, "sqlite")


# ---------------------------------------------------------------------------
# claim_events — immutable audit trail (from_state -> to_state), never updated
# ---------------------------------------------------------------------------
class ClaimEventRow(Base):
    __tablename__ = "claim_events"

    claim: Mapped["ClaimRow"] = relationship(lazy="raise")

    id: Mapped[int] = mapped_column(_INT_PK, primary_key=True, autoincrement=True)
    claim_id: Mapped[str] = mapped_column(ForeignKey("claims.claim_id"), nullable=False)
    from_state: Mapped[Optional[str]] = mapped_column(String(32))
    to_state: Mapped[str] = mapped_column(String(32), nullable=False)
    actor: Mapped[str] = mapped_column(String(64), nullable=False, default="system")
    reason_codes: Mapped[List[str]] = mapped_column(JSON_VARIANT, default=list)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (Index("ix_claim_events_claim", "claim_id"),)


# ---------------------------------------------------------------------------
# refunds — one refund per claim (UNIQUE claim_id = idempotency line of defense)
# ---------------------------------------------------------------------------
class RefundRow(Base):
    __tablename__ = "refunds"

    claim: Mapped["ClaimRow"] = relationship(lazy="raise")

    refund_id: Mapped[str] = mapped_column(String(64), primary_key=True)  # PayPal refund id
    claim_id: Mapped[str] = mapped_column(
        ForeignKey("claims.claim_id"), nullable=False, unique=True
    )
    amount: Mapped[Decimal] = mapped_column(MoneyAmount(14, 2), nullable=False)
    currency_code: Mapped[str] = mapped_column(String(3), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


# ---------------------------------------------------------------------------
# ledger_entries — internal split: MERCHANT_REVENUE | PROTECTION_FEE | REFUND
# (architecturally simulates payment_instruction.platform_fees)
# ---------------------------------------------------------------------------
class LedgerEntryRow(Base):
    __tablename__ = "ledger_entries"

    order: Mapped["OrderRow"] = relationship(lazy="raise")

    id: Mapped[int] = mapped_column(_INT_PK, primary_key=True, autoincrement=True)
    internal_order_id: Mapped[str] = mapped_column(
        ForeignKey("orders.internal_order_id"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    amount: Mapped[Decimal] = mapped_column(MoneyAmount(14, 2), nullable=False)
    currency_code: Mapped[str] = mapped_column(String(3), nullable=False)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        CheckConstraint(
            "kind IN ('MERCHANT_REVENUE', 'PROTECTION_FEE', 'REFUND')",
            name="ck_ledger_kind",
        ),
        Index("ix_ledger_order_id", "internal_order_id"),
    )
