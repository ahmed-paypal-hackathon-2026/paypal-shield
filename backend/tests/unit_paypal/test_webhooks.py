"""Webhook ingestion + processing (Person 1, spec sections 2.2 / 2.6).

Covers: signature verification (real path + UNVERIFIED_DEMO dev path),
event-id dedupe (insert-first unique), PAYMENT.CAPTURE.COMPLETED reconcile,
PAYMENT.CAPTURE.REFUNDED confirm, CUSTOMER.DISPUTE.CREATED flag, unknown
events ignored, processed_at marking.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import Settings
from app.db.base import Base
from app.db.models import (
    ClaimRow,
    LedgerEntryRow,
    MerchantReputationRow,
    OrderRow,
    RefundRow,
    WebhookEventRow,
)
from app.services import webhooks
from app.services.webhooks import (
    HANDLED_EVENT_TYPES,
    WebhookVerificationError,
    extract_event_id,
    mark_processed,
    process_event,
    store_event,
    verify_webhook_signature,
)

NOW = datetime.now(timezone.utc)

_TEST_NS = uuid.uuid5(uuid.NAMESPACE_URL, "paypal-shield-tests")


def _oid(name: str) -> uuid.UUID:
    """Deterministic internal order id (matches the Uuid(as_uuid=True) pk)."""
    return uuid.uuid5(_TEST_NS, f"order:{name}")


def _settings(**kw) -> Settings:
    base = dict(quotes_hmac_key="wh-test-key-0123456789abcdef", _env_file=None)
    base.update(kw)
    return Settings(**base)


class FakeTransport:
    def __init__(self, plan=None):
        self.calls: list[dict] = []
        self.plan = plan or {}

    def on(self, key, action):
        self.plan[key] = action
        return self

    async def request(self, method, path, *, request_id, json_body=None):
        self.calls.append({"method": method, "path": path, "request_id": request_id, "body": json_body})
        for pattern, action in self.plan.items():
            if pattern in path:
                if isinstance(action, Exception):
                    raise action
                return action
        raise AssertionError(f"unexpected paypal call: {method} {path}")


VERIFY_OK = {"verification_status": "SUCCESS"}
VERIFY_FAIL = {"verification_status": "FAILURE"}


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


async def _seed_captured_order(db, internal="ord_wh1", state="CAPTURED", capture="CAP-WH1"):
    async with db() as s:
        s.add(MerchantReputationRow(merchant_id="mer_wh"))
        s.add(
            OrderRow(
                internal_order_id=_oid(internal),
                quote_id="q_wh1",
                paypal_order_id=f"PAY-{internal}",
                paypal_capture_id=capture if state != "PENDING_RECONCILIATION" else None,
                merchant_id="mer_wh",
                buyer_ref="buyer_wh",
                opted_in=True,
                item_price=Decimal("120.00"),
                protection_fee=Decimal("2.70"),
                currency_code="USD",
                state=state,
                created_at=NOW - timedelta(hours=1),
            )
        )
        await s.commit()
    return _oid(internal)


CAPTURE_COMPLETE_EVENT = {
    "event_type": "PAYMENT.CAPTURE.COMPLETED",
    "resource": {
        "id": "CAP-NEW",
        "custom_id": str(_oid("ord_wh1")),
        "status": "COMPLETED",
    },
}


class TestVerification:
    async def test_unverified_demo_when_no_webhook_id(self):
        """No configured webhook id (dev/demo): accept with loud label."""
        result = await verify_webhook_signature(
            FakeTransport(), _settings(), headers={}, raw_body=b"{}"
        )
        assert result == "UNVERIFIED_DEMO"

    async def test_verified_via_paypal_endpoint(self):
        t = FakeTransport().on("verify-webhook-signature", VERIFY_OK)
        settings = _settings(
            paypal_client_id="cid", paypal_client_secret="sec", paypal_webhook_id="WHID-1"
        )
        result = await verify_webhook_signature(
            t,
            settings,
            headers={
                "PAYPAL-AUTH-ALGO": "SHA256withRSA",
                "PAYPAL-CERT-URL": "https://sandbox.paypal.com/cert",
                "PAYPAL-TRANSMISSION-ID": "TXN-1",
                "PAYPAL-TRANSMISSION-SIG": "sig-blob",
                "PAYPAL-TRANSMISSION-TIME": "2026-01-01T00:00:00Z",
            },
            raw_body=b'{"id": "WH-1", "event_type": "X"}',
        )
        assert result == "VERIFIED"
        assert len(t.calls) == 1
        body = t.calls[0]["body"]
        assert body["webhook_id"] == "WHID-1"
        assert body["transmission_id"] == "TXN-1"
        assert body["webhook_event"] == {"id": "WH-1", "event_type": "X"}

    async def test_failed_signature_raises(self):
        t = FakeTransport().on("verify-webhook-signature", VERIFY_FAIL)
        settings = _settings(
            paypal_client_id="cid", paypal_client_secret="sec", paypal_webhook_id="WHID-1"
        )
        with pytest.raises(WebhookVerificationError):
            await verify_webhook_signature(
                t,
                settings,
                headers={"PAYPAL-TRANSMISSION-ID": "TXN-2"},
                raw_body=b"{}",
            )

    def test_handled_event_types_per_spec(self):
        assert HANDLED_EVENT_TYPES == frozenset(
            {
                "PAYMENT.CAPTURE.COMPLETED",
                "PAYMENT.CAPTURE.REFUNDED",
                "CUSTOMER.DISPUTE.CREATED",
            }
        )


class TestDedupe:
    async def test_store_event_insert_first_then_duplicate(self, db):
        async with db() as s:
            fresh = await store_event(s, "TXN-D1", "T", {"a": 1})
            await s.commit()
        assert fresh is True
        async with db() as s:
            again = await store_event(s, "TXN-D1", "T", {"a": 1})
            await s.rollback()
        assert again is False

    async def test_store_event_unique_across_types(self, db):
        """Same event id delivered twice with different payloads -> still dup."""
        async with db() as s:
            assert await store_event(s, "TXN-D2", "A", {"a": 1}) is True
            await s.commit()
        async with db() as s:
            again = await store_event(s, "TXN-D2", "B", {"b": 2})
            await s.rollback()
            assert again is False

    def test_extract_event_id_prefers_transmission_header(self):
        assert extract_event_id({"PAYPAL-TRANSMISSION-ID": "TXN-H"}, {"id": "WH-P"}) == "TXN-H"
        assert extract_event_id({}, {"id": "WH-P"}) == "WH-P"
        assert extract_event_id({}, {}) == ""


class TestCaptureCompletedReconcile:
    async def test_reconciles_limbo_order_and_ledgers(self, db):
        await _seed_captured_order(
            db, internal="ord_wh2", state="PENDING_RECONCILIATION", capture=None
        )
        event = {
            "event_type": "PAYMENT.CAPTURE.COMPLETED",
            "resource": {"id": "CAP-NEW", "custom_id": str(_oid("ord_wh2")), "status": "COMPLETED"},
        }
        async with db() as s:
            outcome = await process_event(s, event)
            await s.commit()  # background tasks commit; mirror that here
        assert outcome == "reconciled"
        async with db() as s:
            order = await s.get(OrderRow, _oid("ord_wh2"))
            assert order.state == "CAPTURED"
            assert order.paypal_capture_id == "CAP-NEW"
            kinds = {k for (k,) in await s.execute(select(LedgerEntryRow.kind))}
            assert kinds == {"MERCHANT_REVENUE", "PROTECTION_FEE"}
            rep = await s.get(MerchantReputationRow, "mer_wh")
            assert rep.total_orders == 1 and rep.successful_orders == 1

    async def test_never_downgrades_already_captured(self, db):
        await _seed_captured_order(db, internal="ord_wh3", state="CAPTURED", capture="CAP-OLD")
        # ledger pre-posted exactly as capture flow would have
        event = {
            "event_type": "PAYMENT.CAPTURE.COMPLETED",
            "resource": {"id": "CAP-OLD", "custom_id": str(_oid("ord_wh3")), "status": "COMPLETED"},
        }
        async with db() as s:
            outcome = await process_event(s, event)
        assert outcome == "noop"
        async with db() as s:
            order = await s.get(OrderRow, _oid("ord_wh3"))
            assert order.state == "CAPTURED"
            kinds = {k for (k,) in await s.execute(select(LedgerEntryRow.kind))}
            assert kinds == set()  # no double-posted entries

    async def test_unmatched_event_stores_without_effects(self, db):
        event = {
            "event_type": "PAYMENT.CAPTURE.COMPLETED",
            "resource": {"id": "CAP-NOPE", "status": "COMPLETED"},
        }
        async with db() as s:
            outcome = await process_event(s, event)
        assert outcome == "unmatched"


class TestRefundConfirm:
    async def _seed_pending_refund(self, db, claim_id: uuid.UUID):
        async with db() as s:
            await _seed_captured_order(db, internal="ord_r1", capture="CAP-R1")
            s.add(
                ClaimRow(
                    claim_id=claim_id,
                    internal_order_id=_oid("ord_r1"),
                    state="AUTO_APPROVED",
                    claim_amount=Decimal("120.00"),
                    currency_code="USD",
                    received_image_hash="h" * 32,
                )
            )
            s.add(
                RefundRow(
                    refund_id="RFX-1",
                    claim_id=claim_id,
                    amount=Decimal("120.00"),
                    currency_code="USD",
                    status="PENDING",
                )
            )
            await s.commit()

    async def test_confirm_completes_pending_refund_closed_loop(self, db):
        cid = uuid.uuid4()
        await self._seed_pending_refund(db, cid)
        event = {
            "event_type": "PAYMENT.CAPTURE.REFUNDED",
            "resource": {"id": "RFX-1", "custom_id": str(cid), "status": "COMPLETED"},
        }
        async with db() as s:
            outcome = await process_event(s, event)
        assert outcome == "refund_confirmed"
        async with db() as s:
            claim = await s.get(ClaimRow, cid)
            assert claim.state == "REFUNDED"
            order = await s.get(OrderRow, _oid("ord_r1"))
            assert order.state == "REFUNDED"
            kinds = {k for (k,) in await s.execute(select(LedgerEntryRow.kind))}
            assert "REFUND" in kinds
            rep = await s.get(MerchantReputationRow, "mer_wh")
            assert rep.verified_fraud_claims == 1

    async def test_redelivery_is_idempotent(self, db):
        cid = uuid.uuid4()
        await self._seed_pending_refund(db, cid)
        event = {
            "event_type": "PAYMENT.CAPTURE.REFUNDED",
            "resource": {"id": "RFX-1", "custom_id": str(cid), "status": "COMPLETED"},
        }
        async with db() as s:
            assert await process_event(s, event) == "refund_confirmed"
        async with db() as s:
            assert await process_event(s, event) == "noop"  # already REFUNDED
        async with db() as s:
            rep = await s.get(MerchantReputationRow, "mer_wh")
            assert rep.verified_fraud_claims == 1  # never double-counted

    async def test_unknown_refund_matches_nothing(self, db):
        event = {
            "event_type": "PAYMENT.CAPTURE.REFUNDED",
            "resource": {"id": "RFX-NOPE", "status": "COMPLETED"},
        }
        async with db() as s:
            assert await process_event(s, event) == "unmatched"


class TestDisputeFlag:
    async def test_dispute_sets_flag_idempotent(self, db):
        internal = _oid("ord_dp")
        await _seed_captured_order(db, internal="ord_dp")
        event = {
            "event_type": "CUSTOMER.DISPUTE.CREATED",
            "resource": {"id": "DP-1", "custom_id": str(internal), "status": "OPEN"},
        }
        async with db() as s:
            assert await process_event(s, event) == "dispute_flagged"
            await s.commit()
        async with db() as s:
            order = await s.get(OrderRow, internal)
            assert order.dispute_active is True
        async with db() as s:
            assert await process_event(s, event) == "dispute_flagged"  # idempotent set
            await s.commit()
        async with db() as s:
            order = await s.get(OrderRow, internal)
            assert order.dispute_active is True

    async def test_unmatched_dispute_stored_only(self, db):
        event = {
            "event_type": "CUSTOMER.DISPUTE.CREATED",
            "resource": {"id": "DP-NOPE", "status": "OPEN"},
        }
        async with db() as s:
            assert await process_event(s, event) == "unmatched"

    async def test_real_disputed_transactions_shape_matches_capture(self, db):
        internal = await _seed_captured_order(
            db, internal="ord_dp_shape", capture="CAP-DP-SHAPE"
        )
        event = {
            "event_type": "CUSTOMER.DISPUTE.CREATED",
            "resource": {
                "id": "PP-D-123",
                "disputed_transactions": [
                    {"seller_transaction_id": "CAP-DP-SHAPE"}
                ],
            },
        }
        async with db() as s:
            assert await process_event(s, event) == "dispute_flagged"
            await s.commit()
        async with db() as s:
            order = await s.get(OrderRow, internal)
            assert order.dispute_active is True


class TestIgnoreAndProcessingState:
    async def test_unhandled_event_types_ignored(self, db):
        async with db() as s:
            assert await process_event(s, {"event_type": "CHECKOUT.ORDER.APPROVED"}) == "ignored"
            assert await process_event(s, {"event_type": "PAYMENT.CAPTURE.DENIED"}) == "ignored"

    async def test_mark_processed(self, db):
        async with db() as s:
            await store_event(s, "TXN-MP1", "T", {"a": 1})
            await s.commit()
        async with db() as s:
            pending = await s.get(WebhookEventRow, "TXN-MP1")
            assert pending.processed_at is None
        async with db() as s:
            await mark_processed(s, "TXN-MP1")
            await s.commit()
        async with db() as s:
            done = await s.get(WebhookEventRow, "TXN-MP1")
            assert done.processed_at is not None
