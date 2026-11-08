"""Database layer property tests (Person 1).

- Money never round-trips through float (in-memory SQLite + canonical strings).
- Unique constraints give the second line of idempotency defense:
    * refunds.claim_id (one refund per claim)
    * claims.internal_order_id (one claim per order)
    * orders.paypal_order_id / paypal_capture_id
    * merchant_reputation CHECK successful_orders <= total_orders
    * ledger_entries CHECK kind IN (...)
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.session import _register_sqlite_pragmas
from app.db.models import (
    ClaimRow,
    LedgerEntryRow,
    MerchantReputationRow,
    OrderRow,
    QuoteRow,
    RefundRow,
    RiskCacheRow,
)


@pytest.fixture
async def db():
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    _register_sqlite_pragmas(engine)  # foreign_keys=ON for FK tests
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest.fixture
async def session(db):
    async with db() as s:
        yield s


async def _seed_merchant(s, merchant_id="mer_demo"):
    s.add(MerchantReputationRow(merchant_id=merchant_id))
    await s.flush()
    return merchant_id


async def _add_quote(s, merchant="mer_demo", quote_id="q_1", currency="USD",
                     price="120.00", fee="0.47"):
    s.add(
        QuoteRow(
            quote_id=quote_id, merchant_id=merchant, item_hash="a" * 64,
            item_price=Decimal(price), currency_code=currency,
            protection_fee=Decimal(fee), r_final=Decimal("7.80"),
            expires_at=_future(), hmac_signature="0" * 64,
        )
    )
    await s.flush()


def _future():
    from datetime import datetime, timedelta, timezone

    return datetime.now(timezone.utc) + timedelta(seconds=600)


async def _captured_order(db, order_id=None):
    """Merchant + quote + CAPTURED order; returns internal order id."""
    from uuid import uuid4

    oid = order_id or uuid4()
    async with db() as s:
        await _seed_merchant(s)
        await _add_quote(s)
        s.add(OrderRow(
            internal_order_id=oid, quote_id="q_1", paypal_order_id="5OY1",
            paypal_capture_id="8XC1", merchant_id="mer_demo",
            buyer_ref="buyer", opted_in=True,
            item_price=Decimal("120.00"), protection_fee=Decimal("0.47"),
            currency_code="USD", state="CAPTURED",
        ))
        await s.commit()
    return oid


class TestMoneyNeverFloat:
    async def test_decimal_roundtrip_cent_exact(self, db):
        async with db() as s:
            m = await _seed_merchant(s)
            await _add_quote(s)
            await s.commit()

        async with db() as s:
            row = (
                await s.execute(select(QuoteRow).where(QuoteRow.quote_id == "q_1"))
            ).scalar_one()
            assert isinstance(row.item_price, Decimal)
            assert row.item_price == Decimal("120.00")
            assert row.protection_fee == Decimal("0.47")
            assert str(row.item_price) == "120.00"  # exactly, not 120.0000000001

    async def test_zero_decimal_currency_stays_integral(self, db):
        async with db() as s:
            m = await _seed_merchant(s)
            await _add_quote(s, quote_id="q_jpy", currency="JPY", price="8000", fee="35")
            await s.commit()

        async with db() as s:
            row = (await s.execute(select(QuoteRow).where(QuoteRow.quote_id == "q_jpy"))).scalar_one()
            assert isinstance(row.protection_fee, Decimal)
            assert row.protection_fee == Decimal("35")  # no 35.00 float leakage
            assert str(row.protection_fee) == "35"


class TestIdempotencyConstraints:
    async def test_one_refund_per_claim(self, db):
        from uuid import uuid4

        oid = await _captured_order(db)
        async with db() as s:
            s.add(ClaimRow(
                claim_id=uuid4(), internal_order_id=oid, state="AUTO_APPROVED",
                claim_amount=Decimal("120.00"), currency_code="USD",
                received_image_hash="c" * 64,
            ))
            await s.commit()

        # The claim id is stable across retries; second insert must fail.
        async with db() as s:
            row = (await s.execute(select(ClaimRow))).scalar_one()
            s.add(RefundRow(refund_id="R1", claim_id=row.claim_id,
                            amount=Decimal("120.00"), currency_code="USD", status="COMPLETED"))
            await s.commit()

        async with db() as s:
            row = (await s.execute(select(ClaimRow))).scalar_one()
            s.add(RefundRow(refund_id="R2", claim_id=row.claim_id,
                            amount=Decimal("120.00"), currency_code="USD", status="COMPLETED"))
            with pytest.raises(IntegrityError):
                await s.commit()

    async def test_one_claim_per_order(self, db):
        from uuid import uuid4

        oid = await _captured_order(db)
        async with db() as s:
            s.add(ClaimRow(claim_id=uuid4(), internal_order_id=oid,
                           state="SUBMITTED", claim_amount=Decimal("120.00"),
                           currency_code="USD"))
            await s.commit()
        async with db() as s:
            s.add(ClaimRow(claim_id=uuid4(), internal_order_id=oid,
                           state="SUBMITTED", claim_amount=Decimal("120.00"),
                           currency_code="USD"))
            with pytest.raises(IntegrityError):
                await s.commit()

    async def test_unique_paypal_order_id(self, db):
        from uuid import uuid4

        await _captured_order(db)
        async with db() as s:
            # merchant already exists from _captured_order
            s.add(OrderRow(
                internal_order_id=uuid4(), quote_id="q_1", paypal_order_id="5OY1",
                merchant_id="mer_demo", buyer_ref="x", opted_in=True,
                item_price=Decimal("120.00"), protection_fee=Decimal("0.47"),
                currency_code="USD", state="ORDER_CREATED",
            ))
            with pytest.raises(IntegrityError):
                await s.commit()

    async def test_reputation_success_le_total_check(self, db):
        async with db() as s:
            s.add(MerchantReputationRow(merchant_id="bad", total_orders=2, successful_orders=5))
            with pytest.raises(IntegrityError):
                await s.commit()

    async def test_ledger_kind_check(self, db):
        from uuid import uuid4

        oid = await _captured_order(db)
        async with db() as s:
            s.add(LedgerEntryRow(
                internal_order_id=oid, kind="INSURANCE_PAYOUT",  # forbidden word AND kind
                amount=Decimal("1.00"), currency_code="USD",
            ))
            with pytest.raises(IntegrityError):
                await s.commit()

    async def test_foreign_key_quote_required(self, db):
        from uuid import uuid4

        async with db() as s:
            await _seed_merchant(s)
            s.add(OrderRow(
                internal_order_id=uuid4(), quote_id="no_such_quote",
                merchant_id="mer_demo", buyer_ref="x", opted_in=True,
                item_price=Decimal("1.00"), protection_fee=Decimal("0"),
                currency_code="USD", state="QUOTED",
            ))
            with pytest.raises(IntegrityError):
                await s.commit()


class TestRiskCacheAndEvents:
    async def test_risk_cache_ttl_row_roundtrip(self, db):
        async with db() as s:
            s.add(RiskCacheRow(
                image_hash="a" * 64, text_hash="b" * 64,
                pillars_json={"s_visual": "0.10"},
                model_versions={"visual": "v1"}, expires_at=_future(),
            ))
            await s.commit()
            row = (await s.execute(select(RiskCacheRow))).scalar_one()
            assert row.pillars_json["s_visual"] == "0.10"

    async def test_claim_events_append_only_shape(self, db):
        from uuid import uuid4

        oid = await _captured_order(db)
        async with db() as s:
            claim = ClaimRow(
                claim_id=uuid4(), internal_order_id=oid, state="SUBMITTED",
                claim_amount=Decimal("120.00"), currency_code="USD",
            )
            s.add(claim)
            from app.db.models import ClaimEventRow

            s.add(ClaimEventRow(
                claim_id=claim.claim_id, from_state=None, to_state="SUBMITTED",
                actor="system", reason_codes=[],
            ))
            await s.commit()
            events = (await s.execute(select(ClaimEventRow))).scalars().all()
            assert len(events) == 1
            assert events[0].id == 1


class TestSingleFlushOrdering:
    """Regression: several related rows inserted in ONE flush (SQLAlchemy UOW
    orders only via declared relationships) - payment transactions rely on this."""

    async def test_full_chain_in_one_flush(self, db):
        from uuid import uuid4
        from datetime import datetime, timedelta, timezone

        from app.db.models import ClaimEventRow, OrderRow, QuoteRow, RefundRow

        cid = uuid4()
        oid = uuid4()
        async with db() as s:
            s.add(MerchantReputationRow(merchant_id="mer_full"))
            s.add(QuoteRow(
                quote_id="q_full", merchant_id="mer_full", item_hash="a" * 64,
                item_price=Decimal("120.00"), currency_code="USD",
                protection_fee=Decimal("0.47"), r_final=Decimal("7.80"),
                expires_at=datetime.now(timezone.utc) + timedelta(seconds=600),
                hmac_signature="0" * 64,
            ))
            s.add(OrderRow(
                internal_order_id=oid, quote_id="q_full",
                paypal_order_id="PO1", paypal_capture_id="PC1",
                merchant_id="mer_full", buyer_ref="b", opted_in=True,
                item_price=Decimal("120.00"), protection_fee=Decimal("0.47"),
                currency_code="USD", state="CAPTURED",
            ))
            s.add(ClaimRow(
                claim_id=cid, internal_order_id=oid, state="AUTO_APPROVED",
                claim_amount=Decimal("120.00"), currency_code="USD",
            ))
            s.add(ClaimEventRow(
                claim_id=cid, from_state="AUTO_APPROVED", to_state="REFUNDED",
                actor="system", reason_codes=["REFUND_COMPLETED"],
            ))
            s.add(RefundRow(
                refund_id="R_full", claim_id=cid, amount=Decimal("120.00"),
                currency_code="USD", status="COMPLETED",
            ))
            await s.commit()  # no explicit flush(): ordering must be FK-correct

        async with db() as s:
            n_orders = len((await s.execute(select(OrderRow))).scalars().all())
            n_events = len((await s.execute(select(ClaimEventRow))).scalars().all())
            n_refunds = len((await s.execute(select(RefundRow))).scalars().all())
            assert (n_orders, n_events, n_refunds) == (1, 1, 1)
