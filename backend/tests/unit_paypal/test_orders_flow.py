"""Order lifecycle flow (Person 1, spec section 3.3).

Real in-memory DB + fake PayPal transport for the create/capture state
machine: happy paths, replays, tampering, consumed-flip, reconciliation.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.config import Settings
from app.db.base import Base
from app.db.models import (
    LedgerEntryRow,
    MerchantReputationRow,
    OrderRow,
    QuoteRow,
)
from app.services import orders_flow
from app.services.paypal.errors import (
    IllegalOrderTransition,
    OrderNotFoundError,
    OrderPayloadError,
    PayPalClientError,
    PayPalTransientError,
)
from app.services.quotes import (
    QuoteConsumedError,
    QuoteExpiredError,
    QuoteSignatureError,
    canonical_quote_string,
    sign_canonical,
)

HMAC_KEY = "flow-test-key-0123456789abcdef"
NOW = datetime.now(timezone.utc)


def _settings(**kw) -> Settings:
    base = dict(quotes_hmac_key=HMAC_KEY, _env_file=None)
    base.update(kw)
    return Settings(**base)


class FakeTransport:
    """Records calls; returns canned responses; scriptable failures."""

    def __init__(self, plan=None):
        self.calls: list[dict] = []
        self.plan = plan or {}

    def on(self, key: str, action):
        self.plan[key] = action
        return self

    async def request(self, method, path, *, request_id, json_body=None):
        self.calls.append(
            {"method": method, "path": path, "request_id": request_id, "body": json_body}
        )
        for pattern, action in self.plan.items():
            if pattern in path:
                if isinstance(action, Exception):
                    raise action
                return action
        raise AssertionError(f"unexpected paypal call: {method} {path}")


CAPTURE_OK = {
    "status": "COMPLETED",
    "purchase_units": [
        {"payments": {"captures": [{"id": "CAP-1", "status": "COMPLETED"}]}}
    ],
}


@pytest.fixture
async def db():
    engine = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _seed_quote(
    db,
    quote_id="q_flow1",
    merchant="mer_flow",
    price="120.00",
    fee="2.70",
    currency="USD",
    expired=False,
    tamper_sig=False,
    consumed=False,
):
    item_hash = "a" * 64
    item_price = Decimal(price)
    protection_fee = Decimal(fee)
    expires_at = NOW - timedelta(seconds=1) if expired else NOW + timedelta(seconds=600)
    canonical = canonical_quote_string(
        quote_id=quote_id,
        merchant_id=merchant,
        item_hash=item_hash,
        item_price=item_price,
        currency_code=currency,
        protection_fee=protection_fee,
        expires_at=expires_at,
    )
    sig = "f" * 64 if tamper_sig else sign_canonical(canonical, [HMAC_KEY])
    async with db() as s:
        s.add(MerchantReputationRow(merchant_id=merchant))
        s.add(
            QuoteRow(
                quote_id=quote_id, merchant_id=merchant, item_hash=item_hash,
                item_price=item_price, currency_code=currency,
                protection_fee=protection_fee, r_final=Decimal("45.00"),
                expires_at=expires_at, hmac_signature=sig, consumed=consumed,
            )
        )
        await s.commit()


class TestCreateOrder:
    async def test_happy_path_opt_in(self, db):
        await _seed_quote(db)
        t = FakeTransport()
        t.on("/v2/checkout/orders", {"id": "PAY-Q1", "status": "CREATED"})
        async with db() as s:
            resp = await orders_flow.create_order(
                s, t, _settings(), quote_id="q_flow1", buyer_ref="buyer1", opt_in=True,
            )
            await s.commit()
        assert resp.order_id == "PAY-Q1"
        assert resp.status == "ORDER_CREATED"
        assert resp.total == Decimal("122.70")
        assert resp.currency_code == "USD"
        assert len(t.calls) == 1
        call = t.calls[0]
        assert call["method"] == "POST"
        assert call["path"] == "/v2/checkout/orders"
        # deterministic request id from the quote
        assert (
            call["request_id"]
            == orders_flow.request_id_for_quote("q_flow1")
        )
        # fee line present exactly when opted in
        items = call["body"]["purchase_units"][0]["items"]
        assert len(items) == 2
        assert items[1]["name"].startswith("Buyer Protection Fee")

    async def test_replay_is_free_no_new_paypal_call(self, db):
        await _seed_quote(db)
        t = FakeTransport()
        t.on("/v2/checkout/orders", {"id": "PAY-Q1", "status": "CREATED"})
        async with db() as s:
            await orders_flow.create_order(
                s, t, _settings(), quote_id="q_flow1", buyer_ref="buyer1", opt_in=True,
            )
            await s.commit()
        t2 = FakeTransport()
        async with db() as s:
            resp2 = await orders_flow.create_order(
                s, t2, _settings(), quote_id="q_flow1", buyer_ref="buyer1", opt_in=True,
            )
            await s.commit()
        assert t2.calls == [], "identical retry must not touch PayPal"
        assert resp2.order_id == "PAY-Q1"
        assert resp2.status == "ORDER_CREATED"

    async def test_opt_out_no_fee_line_no_consumed(self, db):
        await _seed_quote(db)
        t = FakeTransport()
        t.on("/v2/checkout/orders", {"id": "PAY-Q2", "status": "CREATED"})
        async with db() as s:
            resp = await orders_flow.create_order(
                s, t, _settings(), quote_id="q_flow1", buyer_ref="buyer2", opt_in=False,
            )
            await s.commit()
            assert resp.total == Decimal("120.00")  # strictly item price
            row = await s.get(OrderRow, uuid.UUID(orders_flow.internal_order_id_for("q_flow1", "buyer2")))
            assert row.protection_fee == Decimal("0")
            assert row.opted_in is False
            assert row.state == "ORDER_CREATED"
            qrow = await s.get(QuoteRow, "q_flow1")
            assert qrow.consumed is False, "non-opt-in leaves the quote reusable"

    async def test_opt_in_marks_quote_consumed(self, db):
        await _seed_quote(db)
        t = FakeTransport()
        t.on("/v2/checkout/orders", {"id": "PAY-Q3", "status": "CREATED"})
        async with db() as s:
            await orders_flow.create_order(
                s, t, _settings(), quote_id="q_flow1", buyer_ref="b3", opt_in=True,
            )
            await s.commit()
        async with db() as s:
            row = await s.get(QuoteRow, "q_flow1")
            assert row.consumed is True

    async def test_consumed_quote_rejected(self, db):
        await _seed_quote(db, consumed=True)
        t = FakeTransport()
        async with db() as s:
            with pytest.raises(QuoteConsumedError):
                await orders_flow.create_order(
                    s, t, _settings(), quote_id="q_flow1", buyer_ref="b4", opt_in=True,
                )
        assert t.calls == []

    async def test_expired_quote_rejected(self, db):
        await _seed_quote(db, expired=True)
        t = FakeTransport()
        async with db() as s:
            with pytest.raises(QuoteExpiredError):
                await orders_flow.create_order(
                    s, t, _settings(), quote_id="q_flow1", buyer_ref="b5", opt_in=True,
                )
        assert t.calls == []

    async def test_tampered_quote_signature_rejected(self, db):
        await _seed_quote(db, tamper_sig=True)
        t = FakeTransport()
        async with db() as s:
            with pytest.raises(QuoteSignatureError):
                await orders_flow.create_order(
                    s, t, _settings(), quote_id="q_flow1", buyer_ref="b6", opt_in=True,
                )
        assert t.calls == [], "nothing is sent to PayPal on signature failure"

    async def test_different_buyers_cannot_collide_idempotency(self, db):
        await _seed_quote(db)
        t = FakeTransport()
        t.on("/v2/checkout/orders", {"id": "PAY-Q4", "status": "CREATED"})
        async with db() as s:
            await orders_flow.create_order(
                s, t, _settings(), quote_id="q_flow1", buyer_ref="buyerX", opt_in=True,
            )
            await s.commit()
        # same buyer -> replay; different buyer of same consumed quote -> rejected
        async with db() as s:
            resp = await orders_flow.create_order(
                s, t, _settings(), quote_id="q_flow1", buyer_ref="buyerX", opt_in=True,
            )
            await s.commit()
            assert resp.order_id == "PAY-Q4"
            with pytest.raises(QuoteConsumedError):
                await orders_flow.create_order(
                    s, t, _settings(), quote_id="q_flow1", buyer_ref="buyerY", opt_in=True,
                )


class TestCaptureOrder:
    async def _created(self, db, quote_id="q_flow1", buyer="cap1", opt_in=True):
        await _seed_quote(db, quote_id=quote_id)
        t = FakeTransport()
        t.on("/orders", {"id": f"PAY-{quote_id}", "status": "CREATED"})
        async with db() as s:
            await orders_flow.create_order(
                s, t, _settings(), quote_id=quote_id, buyer_ref=buyer, opt_in=opt_in,
            )
            await s.commit()
        return f"PAY-{quote_id}", buyer

    async def test_happy_capture_ledgers_and_reputation(self, db):
        paypal_id, _ = await self._created(db)
        t = FakeTransport()
        t.on("/capture", CAPTURE_OK)
        async with db() as s:
            resp = await orders_flow.capture_order(s, t, _settings(), paypal_order_id=paypal_id)
            await s.commit()
            assert resp.paypal_capture_id == "CAP-1"
            assert resp.status == "COMPLETED"
            order_id = orders_flow.internal_order_id_for("q_flow1", "cap1")
            rows = (await s.execute(
                select(LedgerEntryRow).where(
                    LedgerEntryRow.internal_order_id == uuid.UUID(order_id)
                )
            )).scalars().all()
            kinds = {r.kind: r.amount for r in rows}
            assert kinds["MERCHANT_REVENUE"] == Decimal("120.00")
            assert kinds["PROTECTION_FEE"] == Decimal("2.70")
            rep = await s.get(MerchantReputationRow, "mer_flow")
            assert rep.total_orders == 1
            assert rep.successful_orders == 1
            state = (await s.get(OrderRow, uuid.UUID(order_id))).state
            assert state == "CAPTURED"

    async def test_capture_replay_returns_stored_response(self, db):
        paypal_id, _ = await self._created(db)
        t = FakeTransport()
        t.on("/capture", CAPTURE_OK)
        async with db() as s:
            first = await orders_flow.capture_order(s, t, _settings(), paypal_order_id=paypal_id)
            await s.commit()
        t2 = FakeTransport()
        async with db() as s:
            second = await orders_flow.capture_order(s, t2, _settings(), paypal_order_id=paypal_id)
            await s.commit()
        assert t2.calls == [], "capture replay must not touch PayPal"
        assert first.model_dump() == second.model_dump()

    async def test_capture_unknown_order_404(self, db):
        t = FakeTransport()
        async with db() as s:
            with pytest.raises(OrderNotFoundError):
                await orders_flow.capture_order(s, t, _settings(), paypal_order_id="NOPE")

    async def test_transient_exhaustion_marks_pending_reconciliation(self, db):
        paypal_id, _ = await self._created(db)
        t = FakeTransport()
        t.on("/capture", PayPalTransientError("boom", request_id="x", attempts=3))
        async with db() as s:
            with pytest.raises(PayPalTransientError):
                await orders_flow.capture_order(s, t, _settings(), paypal_order_id=paypal_id)
        async with db() as s:
            order_id = orders_flow.internal_order_id_for("q_flow1", "cap1")
            state = (await s.get(OrderRow, uuid.UUID(order_id))).state
            assert state == "PENDING_RECONCILIATION"

    async def test_already_captured_elsewhere_reconciles(self, db):
        paypal_id, _ = await self._created(db)
        t = FakeTransport()
        t.on(
            "/capture",
            PayPalClientError(
                "conflict", status_code=400, issues=["ORDER_ALREADY_CAPTURED"], debug_id="d"
            ),
        )
        async with db() as s:
            with pytest.raises(PayPalClientError):
                await orders_flow.capture_order(s, t, _settings(), paypal_order_id=paypal_id)
        async with db() as s:
            order_id = orders_flow.internal_order_id_for("q_flow1", "cap1")
            state = (await s.get(OrderRow, uuid.UUID(order_id))).state
            assert state == "PENDING_RECONCILIATION", "foreign capture -> reconcile"

    async def test_pending_reconciliation_retry_can_complete(self, db):
        paypal_id, _ = await self._created(db)
        t = FakeTransport()
        t.on("/capture", PayPalTransientError("boom"))
        async with db() as s:
            with pytest.raises(PayPalTransientError):
                await orders_flow.capture_order(s, t, _settings(), paypal_order_id=paypal_id)
        # Transport recovers; the deterministic request id makes PayPal-side
        # replay safe, and PENDING_RECONCILIATION -> CAPTURED is legal.
        t2 = FakeTransport()
        t2.on("/capture", CAPTURE_OK)
        async with db() as s:
            resp = await orders_flow.capture_order(s, t2, _settings(), paypal_order_id=paypal_id)
            await s.commit()
            assert resp.paypal_capture_id == "CAP-1"
            order_id = orders_flow.internal_order_id_for("q_flow1", "cap1")
            row = await s.get(OrderRow, uuid.UUID(order_id))
            assert row.state == "CAPTURED"
            assert row.paypal_capture_id == "CAP-1"

    async def test_capture_with_tampered_quote_fails_and_records(self, db):
        # Create with a good quote; then corrupt the stored signature.
        paypal_id, _ = await self._created(db)
        async with db() as s:
            q = await s.get(QuoteRow, "q_flow1")
            q.hmac_signature = "0" * 64
            await s.commit()
        t = FakeTransport()
        async with db() as s:
            with pytest.raises(QuoteSignatureError):
                await orders_flow.capture_order(s, t, _settings(), paypal_order_id=paypal_id)
        assert t.calls == []
        async with db() as s:
            order_id = orders_flow.internal_order_id_for("q_flow1", "cap1")
            state = (await s.get(OrderRow, uuid.UUID(order_id))).state
            assert state == "CAPTURE_FAILED"


class TestStateMachine:
    def test_legal_chain(self):
        orders_flow.assert_transition("QUOTED", "ORDER_CREATED")
        orders_flow.assert_transition("ORDER_CREATED", "CAPTURED")
        orders_flow.assert_transition("CAPTURED", "PARTIALLY_REFUNDED")
        orders_flow.assert_transition("CAPTURED", "REFUNDED")
        orders_flow.assert_transition("PENDING_RECONCILIATION", "CAPTURED")
        orders_flow.assert_transition("CAPTURE_FAILED", "CAPTURED")

    def test_illegal_jumps_rejected(self):
        with pytest.raises(IllegalOrderTransition):
            orders_flow.assert_transition("QUOTED", "CAPTURED")
        with pytest.raises(IllegalOrderTransition):
            orders_flow.assert_transition("REFUNDED", "CAPTURED")
        with pytest.raises(IllegalOrderTransition):
            orders_flow.assert_transition("QUOTE_EXPIRED", "CAPTURED")
        with pytest.raises(IllegalOrderTransition):
            orders_flow.assert_transition("CAPTURED", "CAPTURED")
