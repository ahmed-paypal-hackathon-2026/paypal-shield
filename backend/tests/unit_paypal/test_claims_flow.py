"""Claim lifecycle: submission, gates, arbitration, refund closed loop
(Person 1, spec sections 3.4 / 6.6 / 6.7 / 2.7).

Service-level tests over the real state machine with an in-memory SQLite and
scriptable transports, plus one API-level end-to-end through the multipart
router in demo/stub mode (mirrors frontend/src/api/client.ts usage).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool
from starlette.testclient import TestClient

from app.config import Settings
from app.db.base import Base
from app.db.models import (
    ClaimEventRow,
    ClaimRow,
    LedgerEntryRow,
    MerchantReputationRow,
    OrderRow,
    RefundRow,
)
from app.schemas.contracts import ArbiterDecision, ArbiterContext, ClaimRecord, ClaimState
from app.services import claims as claims_service
from app.services.ledger import post_capture_entries
from app.services.claims import (
    ALLOWED_CLAIM_TRANSITIONS,
    ClaimAmountError,
    ClaimNotCapturedError,
    ClaimNotProtectedError,
    ClaimNotFoundError,
    ClaimUploadError,
    ClaimWindowError,
    IllegalClaimTransition,
    run_claim_pipeline,
    submit_claim,
    validate_photo,
)
from app.services.paypal.errors import PayPalClientError, PayPalTransientError
from app.services.paypal.refunds import build_refund_body

NOW = datetime.now(timezone.utc)
_JPEG = b"\xff\xd8\xff\xdb" + b"\x00" * 64


def _settings(**kw) -> Settings:
    base = dict(quotes_hmac_key="claim-test-key-0123456789abcdef", _env_file=None)
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


class AutoApproveEngine:
    """Fake arbiter: all gates pass -> AUTO_APPROVED (for the payout path)."""

    async def arbitrate(self, claim, listing_image, received_image, ctx) -> ArbiterDecision:
        return ArbiterDecision(
            claim_id=claim.claim_id,
            discrepancy_score=Decimal("0.1000"),
            gate_amount_ok=True,
            gate_velocity_ok=True,
            gate_no_dispute=True,
            gate_discrepancy_ok=True,
            outcome=ClaimState.AUTO_APPROVED,
            reason_codes=["ALL_GATES_PASSED"],
        )


_AUTO_NS = uuid.uuid5(uuid.NAMESPACE_URL, "paypal-shield-tests")


def _oid(name: str) -> uuid.UUID:
    return uuid.uuid5(_AUTO_NS, f"order:{name}")


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


async def _seed_captured_order(
    db,
    internal="ord_cl1",
    *,
    buyer="buyer_cl",
    claim_piece=None,
    created_hours_ago=1,
    dispute=False,
    opted_in=True,
):
    async with db() as s:
        s.add(MerchantReputationRow(merchant_id="mer_cl"))
        order = OrderRow(
                internal_order_id=_oid(internal),
                quote_id=f"q_{internal}",
                paypal_order_id=f"PAY-{internal}",
                paypal_capture_id="CAP-CL-1",
                merchant_id="mer_cl",
                buyer_ref=buyer,
                opted_in=opted_in,
                item_price=Decimal("120.00"),
                protection_fee=Decimal("2.70"),
                currency_code="USD",
                state="CAPTURED",
                dispute_active=dispute,
                created_at=NOW - timedelta(hours=created_hours_ago),
            )
        s.add(order)
        await s.flush()
        # A real CAPTURED order already has its revenue/fee ledger split and
        # successful-order counters. Mirror that invariant in claim tests.
        await post_capture_entries(s, order)
        await s.flush()
        for entry in (
            await s.execute(
                select(LedgerEntryRow).where(
                    LedgerEntryRow.internal_order_id == order.internal_order_id
                )
            )
        ).scalars():
            entry.at = NOW - timedelta(hours=created_hours_ago)
        await s.commit()
    return _oid(internal)


# ---------------------------------------------------------------------------
# Upload safety (spec 6.8)
# ---------------------------------------------------------------------------


class TestValidatePhoto:
    def test_jpeg(self):
        assert validate_photo(b"\xff\xd8\xff\xe0xyz", 1024) == "jpeg"

    def test_png(self):
        assert validate_photo(b"\x89PNG\r\n\x1a\n\x00\x00", 1024) == "png"

    def test_webp(self):
        assert validate_photo(b"RIFF\x00\x16\x00\x00WEBPVP8 ", 1024) == "webp"

    def test_riff_without_webp_marker_rejected(self):
        with pytest.raises(ClaimUploadError):
            validate_photo(b"RIFFnoise", 1024)

    def test_plain_text_rejected(self):
        with pytest.raises(ClaimUploadError):
            validate_photo(b"this is definitely not an image", 1024)

    def test_empty_rejected(self):
        with pytest.raises(ClaimUploadError):
            validate_photo(b"", 1024)

    def test_oversize_rejected(self):
        with pytest.raises(ClaimUploadError):
            validate_photo(_JPEG, 16)


class TestRefundPayload:
    def test_claim_is_clamped_to_captured_item_price(self):
        body = build_refund_body(
            claim_id="clm-1",
            item_price=Decimal("120.00"),
            claim_amount=Decimal("150.00"),
            currency_code="USD",
        )
        assert body["amount"] == {"currency_code": "USD", "value": "120.00"}
        assert body["custom_id"] == "clm-1"
        assert "fee" not in json_body_dump(body)

    def test_partial_and_zero_decimal_currency_formatting(self):
        usd = build_refund_body(
            claim_id="clm-2", item_price=Decimal("120.00"),
            claim_amount=Decimal("60"), currency_code="USD",
        )
        jpy = build_refund_body(
            claim_id="clm-3", item_price=Decimal("1200"),
            claim_amount=Decimal("500"), currency_code="JPY",
        )
        assert usd["amount"]["value"] == "60.00"
        assert jpy["amount"]["value"] == "500"

    @pytest.mark.parametrize("bad", [Decimal("0"), Decimal("NaN"), Decimal("Infinity")])
    def test_nonpositive_or_nonfinite_amount_rejected(self, bad):
        with pytest.raises(ValueError):
            build_refund_body(
                claim_id="clm-bad", item_price=Decimal("120.00"),
                claim_amount=bad, currency_code="USD",
            )


# ---------------------------------------------------------------------------
# Submission eligibility + replay
# ---------------------------------------------------------------------------


class TestSubmitClaim:
    async def test_happy_path(self, db):
        oid = await _seed_captured_order(db)
        async with db() as s:
            claim = await submit_claim(
                s, _settings(),
                order_ref=f"PAY-ord_cl1",
                photo_bytes=_JPEG,
                claim_amount=None,
            )
            await s.commit()
        assert claim.state == ClaimState.SUBMITTED.value
        assert claim.claim_amount == Decimal("120.00")  # defaults to item price
        assert claim.received_image_hash  # deterministic digest stored
        assert claim.claim_id
        async with db() as s:
            events = (
                await s.execute(
                    select(ClaimEventRow).where(ClaimEventRow.claim_id == claim.claim_id)
                )
            ).scalars().all()
        assert [(e.from_state, e.to_state) for e in events] == [(None, "SUBMITTED")]

    async def test_replay_returns_same_claim_never_double_keys(self, db):
        await _seed_captured_order(db)
        async with db() as s:
            first = await submit_claim(s, _settings(), order_ref="PAY-ord_cl1", photo_bytes=_JPEG)
            await s.commit()
        async with db() as s:
            second = await submit_claim(
                s, _settings(), order_ref="PAY-ord_cl1", photo_bytes=b"\x89PNG\r\n\x1a\n"
            )
            await s.flush()
        assert first.claim_id == second.claim_id
        assert second.state == ClaimState.SUBMITTED.value  # replayed, untouched

    async def test_unknown_order(self, db):
        async with db() as s:
            with pytest.raises(ClaimNotFoundError):
                await submit_claim(s, _settings(), order_ref="PAY-nope", photo_bytes=_JPEG)

    async def test_non_captured_order_rejected(self, db):
        await _seed_captured_order(db, internal="ord_cl2")
        async with db() as s:
            row = await s.get(OrderRow, _oid("ord_cl2"))
            row.state = "ORDER_CREATED"
            await s.commit()
        async with db() as s:
            with pytest.raises(ClaimNotCapturedError):
                await submit_claim(s, _settings(), order_ref="PAY-ord_cl2", photo_bytes=_JPEG)

    async def test_window_passed_rejected(self, db):
        await _seed_captured_order(db, internal="ord_cl3", created_hours_ago=24 * 31)
        async with db() as s:
            with pytest.raises(ClaimWindowError):
                await submit_claim(s, _settings(), order_ref="PAY-ord_cl3", photo_bytes=_JPEG)

    async def test_window_edge_inside_is_fine(self, db):
        await _seed_captured_order(db, internal="ord_cl4", created_hours_ago=24 * 29)
        async with db() as s:
            claim = await submit_claim(s, _settings(), order_ref="PAY-ord_cl4", photo_bytes=_JPEG)
            await s.commit()
        assert claim.state == ClaimState.SUBMITTED.value

    async def test_amount_above_item_price_rejected(self, db):
        await _seed_captured_order(db, internal="ord_cl5")
        async with db() as s:
            with pytest.raises(ClaimAmountError):
                await submit_claim(
                    s, _settings(), order_ref="PAY-ord_cl5",
                    photo_bytes=_JPEG, claim_amount=Decimal("120.01"),
                )

    async def test_amount_zero_rejected(self, db):
        await _seed_captured_order(db, internal="ord_cl6")
        async with db() as s:
            with pytest.raises(ClaimAmountError):
                await submit_claim(
                    s, _settings(), order_ref="PAY-ord_cl6",
                    photo_bytes=_JPEG, claim_amount=Decimal("0.00"),
                )

    async def test_claim_amount_rounds_half_up_never_through_float(self, db):
        await _seed_captured_order(db, internal="ord_cl_round")
        async with db() as s:
            claim = await submit_claim(
                s, _settings(), order_ref="PAY-ord_cl_round",
                photo_bytes=_JPEG, claim_amount=Decimal("1.005"),
            )
            await s.commit()
        assert claim.claim_amount == Decimal("1.01")

    async def test_bad_upload_rejected_before_lookup(self, db):
        await _seed_captured_order(db, internal="ord_cl7")
        async with db() as s:
            with pytest.raises(ClaimUploadError):
                await submit_claim(s, _settings(), order_ref="PAY-ord_cl7", photo_bytes=b"nope")

    async def test_not_opted_in_rejected(self, db):
        await _seed_captured_order(db, internal="ord_not_opted", opted_in=False)
        async with db() as s:
            with pytest.raises(ClaimNotProtectedError):
                await submit_claim(s, _settings(), order_ref="PAY-ord_not_opted", photo_bytes=_JPEG)


# ---------------------------------------------------------------------------
# Pipeline: evidence -> arbitrate -> refund
# ---------------------------------------------------------------------------


class TestClaimStateMachine:
    def test_transition_table_matches_spec(self):
        assert ALLOWED_CLAIM_TRANSITIONS["SUBMITTED"] == frozenset({"EVIDENCE_CHECKED"})
        assert ALLOWED_CLAIM_TRANSITIONS["EVIDENCE_CHECKED"] == frozenset(
            {"AUTO_APPROVED", "MANUAL_REVIEW", "REJECTED"}
        )
        assert ALLOWED_CLAIM_TRANSITIONS["AUTO_APPROVED"] == frozenset({"REFUNDED", "MANUAL_REVIEW"})
        assert ALLOWED_CLAIM_TRANSITIONS["MANUAL_REVIEW"] == frozenset({"REFUNDED", "REJECTED"})
        assert ALLOWED_CLAIM_TRANSITIONS["REJECTED"] == frozenset()
        assert ALLOWED_CLAIM_TRANSITIONS["REFUNDED"] == frozenset()


class TestPipelineStub:
    async def test_stub_engine_never_auto_approves(self, db, monkeypatch):
        """The deterministic fallback only ever escalates to MANUAL_REVIEW."""
        from app.services.stub_engine import StubRiskEngine

        oid = await _seed_captured_order(db)
        async with db() as s:
            claim = await submit_claim(s, _settings(), order_ref="PAY-ord_cl1", photo_bytes=_JPEG)
            await s.commit()
        monkeypatch.setattr(claims_service, "get_risk_engine", lambda: StubRiskEngine())

        t = FakeTransport()  # must NOT receive a refund call
        await run_claim_pipeline(db, _settings(), t, claim_id=str(claim.claim_id))

        async with db() as s:
            row = await s.get(ClaimRow, claim.claim_id)
            assert row.state == ClaimState.MANUAL_REVIEW.value
            assert row.discrepancy is not None
            tos = [e.to_state for e in (
                await s.execute(
                    select(ClaimEventRow)
                    .where(ClaimEventRow.claim_id == claim.claim_id)
                    .order_by(ClaimEventRow.id)
                )
            ).scalars().all()]
        assert tos == ["SUBMITTED", "EVIDENCE_CHECKED", "MANUAL_REVIEW"]
        assert t.calls == []  # no money moved

    async def test_skips_when_claim_missing_or_done(self, db, monkeypatch):
        _seed_captured_order(db)
        monkeypatch.setattr(
            claims_service, "get_risk_engine", lambda: (_ for _ in ()).throw(AssertionError("no"))
        )
        await run_claim_pipeline(db, _settings(), FakeTransport(), claim_id=str(uuid.uuid4()))
        # no exception -> unknown claim just logged

    async def test_engine_crash_falls_back_never_500s(self, db, monkeypatch):
        oid = await _seed_captured_order(db, internal="ord_cl9")
        async with db() as s:
            claim = await submit_claim(s, _settings(), order_ref="PAY-ord_cl9", photo_bytes=_JPEG)
            await s.commit()

        class ExplodingEngine:
            async def arbitrate(self, *a, **k):
                raise RuntimeError("model exploded")

        monkeypatch.setattr(claims_service, "get_risk_engine", lambda: ExplodingEngine())
        await run_claim_pipeline(db, _settings(), FakeTransport(), claim_id=str(claim.claim_id))
        async with db() as s:
            row = await s.get(ClaimRow, claim.claim_id)
            assert row.state == ClaimState.MANUAL_REVIEW.value  # stub fallback


class TestPipelineAutoRefund:
    REFS = "captures"  # path marker for refund calls

    def _fake_engine(self):
        return AutoApproveEngine()

    async def _seed_submitted(self, db, internal="ord_cl1"):
        await _seed_captured_order(db, internal=internal)
        async with db() as s:
            claim = await submit_claim(
                s, _settings(), order_ref=f"PAY-{internal}", photo_bytes=_JPEG
            )
            await s.commit()
        return claim

    async def test_auto_approved_completed_refund_closed_loop(self, db, monkeypatch):
        claim = await self._seed_submitted(db, internal="ord_cl20")
        t = FakeTransport().on("/refund", {"id": "RF-1", "status": "COMPLETED"})
        monkeypatch.setattr(claims_service, "get_risk_engine", lambda: self._fake_engine())

        await run_claim_pipeline(db, _settings(), t, claim_id=str(claim.claim_id))

        assert len(t.calls) == 1
        call = t.calls[0]
        assert call["method"] == "POST"
        assert f"/v2/payments/captures/CAP-CL-1/refund" in call["path"]
        # deterministic request id from claim_id (spec 2.7)
        assert call["request_id"] == claims_service.request_id_for_refund(str(claim.claim_id))
        body = call["body"]
        assert body["amount"] == {"currency_code": "USD", "value": "120.00"}
        assert body["custom_id"] == str(claim.claim_id)
        assert "fee" not in json_body_dump(body)  # fee never refunded

        async with db() as s:
            row = await s.get(ClaimRow, claim.claim_id)
            assert row.state == ClaimState.REFUNDED.value
            order = await s.get(OrderRow, _oid("ord_cl20"))
            assert order.state == "REFUNDED"
            refunds = (
                await s.execute(select(RefundRow).where(RefundRow.claim_id == claim.claim_id))
            ).scalars().all()
            assert len(refunds) == 1 and refunds[0].status == "COMPLETED"
            assert str(refunds[0].amount) == "120.00"
            kinds = {k for (k,) in await s.execute(
                select(LedgerEntryRow.kind).where(
                    LedgerEntryRow.internal_order_id == _oid("ord_cl20")
                )
            )}
            assert "REFUND" in kinds
            rep = await s.get(MerchantReputationRow, "mer_cl")
            assert rep.verified_fraud_claims == 1  # closed loop counter
            tos = [e.to_state for e in (
                await s.execute(
                    select(ClaimEventRow)
                    .where(ClaimEventRow.claim_id == claim.claim_id)
                    .order_by(ClaimEventRow.id)
                )
            ).scalars().all()]
        assert tos == ["SUBMITTED", "EVIDENCE_CHECKED", "AUTO_APPROVED", "REFUNDED"]

    async def test_pending_refund_awaits_webhook_then_confirms(self, db, monkeypatch):
        claim = await self._seed_submitted(db, internal="ord_cl21")
        t = FakeTransport().on("/refund", {"id": "RF-2", "status": "PENDING"})
        monkeypatch.setattr(claims_service, "get_risk_engine", lambda: self._fake_engine())

        await run_claim_pipeline(db, _settings(), t, claim_id=str(claim.claim_id))

        async with db() as s:
            row = await s.get(ClaimRow, claim.claim_id)
            assert row.state == ClaimState.AUTO_APPROVED.value  # stays pending
            order = await s.get(OrderRow, _oid("ord_cl21"))
            assert order.state == "CAPTURED"  # money not moved yet

        # PAYMENT.CAPTURE.REFUNDED arrives asynchronously -> closed loop
        async with db() as s:
            outcome = await claims_service.confirm_refund_webhook(
                s, {"id": "RF-2", "custom_id": str(claim.claim_id), "status": "COMPLETED"}
            )
            await s.commit()
        assert outcome == "refund_confirmed"
        async with db() as s:
            row = await s.get(ClaimRow, claim.claim_id)
            assert row.state == ClaimState.REFUNDED.value
            rep = await s.get(MerchantReputationRow, "mer_cl")
            assert rep.verified_fraud_claims == 1

    async def test_client_error_maps_to_manual_review_with_issue(self, db, monkeypatch):
        claim = await self._seed_submitted(db, internal="ord_cl22")
        t = FakeTransport().on(
            "/refund",
            PayPalClientError(
                "refund rejected",
                status_code=400,
                issues=["REFUND_NOT_PERMITTED"],
                debug_id="dbg-1",
            ),
        )
        monkeypatch.setattr(claims_service, "get_risk_engine", lambda: self._fake_engine())

        await run_claim_pipeline(db, _settings(), t, claim_id=str(claim.claim_id))
        async with db() as s:
            row = await s.get(ClaimRow, claim.claim_id)
            assert row.state == ClaimState.MANUAL_REVIEW.value
            last = (
                await s.execute(
                    select(ClaimEventRow)
                    .where(ClaimEventRow.claim_id == claim.claim_id)
                    .order_by(ClaimEventRow.id.desc())
                    .limit(1)
                )
            ).scalar_one()
        assert any(c == "REFUND_ISSUE_REFUND_NOT_PERMITTED" for c in last.reason_codes)
        assert any(c.startswith("DEBUG_") for c in last.reason_codes)
        async with db() as s:
            rep = await s.get(MerchantReputationRow, "mer_cl")
            assert rep.verified_fraud_claims == 0  # no payout, no counter bump

    async def test_transient_exhaustion_is_retryable_and_resumes(self, db, monkeypatch):
        claim = await self._seed_submitted(db, internal="ord_cl23")
        t1 = FakeTransport().on("/refund", PayPalTransientError("upstream timeout", attempts=3))
        monkeypatch.setattr(claims_service, "get_risk_engine", lambda: self._fake_engine())

        await run_claim_pipeline(db, _settings(), t1, claim_id=str(claim.claim_id))
        async with db() as s:
            row = await s.get(ClaimRow, claim.claim_id)
            assert row.state == ClaimState.AUTO_APPROVED.value  # retryable, money-safe
            last = (
                await s.execute(
                    select(ClaimEventRow)
                    .where(ClaimEventRow.claim_id == claim.claim_id)
                    .order_by(ClaimEventRow.id.desc())
                    .limit(1)
                )
            ).scalar_one()
            assert "REFUND_TRANSIENT_RETRY_LATER" in last.reason_codes

        # Later retry with a healthy transport resumes from AUTO_APPROVED.
        t2 = FakeTransport().on("/refund", {"id": "RF-3", "status": "COMPLETED"})
        await claims_service.execute_refund(
            db, _settings(), t2, claim_id=str(claim.claim_id), actor="system"
        )
        async with db() as s:
            row = await s.get(ClaimRow, claim.claim_id)
            assert row.state == ClaimState.REFUNDED.value
            assert len(t2.calls) == 1  # retried with the same deterministic id
            assert t2.calls[0]["request_id"] == t1.calls[0]["request_id"]

    async def test_refund_replay_never_calls_paypal_twice(self, db, monkeypatch):
        claim = await self._seed_submitted(db, internal="ord_cl24")
        t = FakeTransport().on("/refund", {"id": "RF-4", "status": "COMPLETED"})
        monkeypatch.setattr(claims_service, "get_risk_engine", lambda: self._fake_engine())
        await run_claim_pipeline(db, _settings(), t, claim_id=str(claim.claim_id))
        assert len(t.calls) == 1

        # Funds already REFUNDED -> replay is a no-op at every layer.
        await claims_service.execute_refund(db, _settings(), t, claim_id=str(claim.claim_id))
        assert len(t.calls) == 1
        async with db() as s:
            rep = await s.get(MerchantReputationRow, "mer_cl")
            assert rep.verified_fraud_claims == 1  # increment happened exactly once

    async def test_committed_auto_approval_resumes_after_worker_restart(self, db):
        claim = await self._seed_submitted(db, internal="ord_cl25")
        async with db() as s:
            row = await s.get(ClaimRow, claim.claim_id)
            row.state = ClaimState.AUTO_APPROVED.value
            row.discrepancy = Decimal("0.9000")
            s.add(
                ClaimEventRow(
                    claim_id=claim.claim_id,
                    from_state="EVIDENCE_CHECKED",
                    to_state="AUTO_APPROVED",
                    actor="system",
                    reason_codes=["ALL_GATES_PASSED"],
                )
            )
            await s.commit()

        t = FakeTransport().on("/refund", {"id": "RF-R1", "status": "COMPLETED"})
        await run_claim_pipeline(db, _settings(), t, claim_id=str(claim.claim_id))
        assert len(t.calls) == 1
        async with db() as s:
            row = await s.get(ClaimRow, claim.claim_id)
            assert row.state == ClaimState.REFUNDED.value

    async def test_configured_cap_can_only_downgrade_approval(self, db, monkeypatch):
        claim = await self._seed_submitted(db, internal="ord_cl26")
        monkeypatch.setattr(claims_service, "get_risk_engine", lambda: self._fake_engine())
        t = FakeTransport()

        await run_claim_pipeline(
            db, _settings(claim_auto_payout_cap=Decimal("50.00")), t,
            claim_id=str(claim.claim_id),
        )

        assert t.calls == []
        async with db() as s:
            row = await s.get(ClaimRow, claim.claim_id)
            assert row.state == ClaimState.MANUAL_REVIEW.value
            event = (
                await s.execute(
                    select(ClaimEventRow)
                    .where(ClaimEventRow.claim_id == claim.claim_id)
                    .order_by(ClaimEventRow.id.desc())
                    .limit(1)
                )
            ).scalar_one()
            assert "AMOUNT_EXCEEDS_LIMIT" in event.reason_codes

    async def test_malformed_paypal_refund_goes_to_manual_review(self, db, monkeypatch):
        claim = await self._seed_submitted(db, internal="ord_cl27")
        monkeypatch.setattr(claims_service, "get_risk_engine", lambda: self._fake_engine())
        t = FakeTransport().on("/refund", {"status": "COMPLETED"})  # no id

        await run_claim_pipeline(db, _settings(), t, claim_id=str(claim.claim_id))

        async with db() as s:
            row = await s.get(ClaimRow, claim.claim_id)
            assert row.state == ClaimState.MANUAL_REVIEW.value
            event = (
                await s.execute(
                    select(ClaimEventRow)
                    .where(ClaimEventRow.claim_id == claim.claim_id)
                    .order_by(ClaimEventRow.id.desc())
                    .limit(1)
                )
            ).scalar_one()
            assert "REFUND_ISSUE_MALFORMED_REFUND_RESPONSE" in event.reason_codes


class TestPartialRefund:
    async def test_partial_refund_marks_order_partially_refunded(self, db, monkeypatch):
        await _seed_captured_order(db, internal="ord_cl30")
        async with db() as s:
            claim = await submit_claim(
                s, _settings(), order_ref="PAY-ord_cl30", photo_bytes=_JPEG,
                claim_amount=Decimal("60.00"),
            )
            await s.commit()

        class HalfApproveEngine(AutoApproveEngine):
            async def arbitrate(self, claim, *a, **k):
                d = await super().arbitrate(claim, *a, **k)
                return d

        monkeypatch.setattr(claims_service, "get_risk_engine", lambda: HalfApproveEngine())
        t = FakeTransport().on("/refund", {"id": "RF-9", "status": "COMPLETED"})
        await run_claim_pipeline(db, _settings(), t, claim_id=str(claim.claim_id))
        async with db() as s:
            order = await s.get(OrderRow, _oid("ord_cl30"))
            assert order.state == "PARTIALLY_REFUNDED"
            refund_row = (
                await s.execute(select(RefundRow).where(RefundRow.claim_id == claim.claim_id))
            ).scalar_one()
            assert str(refund_row.amount) == "60.00"
            kinds = {k for (k,) in await s.execute(
                select(LedgerEntryRow.kind).where(
                    LedgerEntryRow.internal_order_id == _oid("ord_cl30")
                )
            )}
            assert kinds == {"MERCHANT_REVENUE", "PROTECTION_FEE", "REFUND"}


# ---------------------------------------------------------------------------
# Reviewer actions
# ---------------------------------------------------------------------------


class TestResolveClaim:
    async def _seed_manual_review(self, db, internal="ord_cl40") -> str:
        await _seed_captured_order(db, internal=internal)
        async with db() as s:
            claim = await submit_claim(s, _settings(), order_ref=f"PAY-{internal}", photo_bytes=_JPEG)
            claim.state = ClaimState.MANUAL_REVIEW.value
            claim.discrepancy = Decimal("0.5000")
            s.add(
                ClaimEventRow(
                    claim_id=claim.claim_id, from_state="EVIDENCE_CHECKED",
                    to_state="MANUAL_REVIEW", actor="system",
                    reason_codes=["STUB_ARBITER_MANUAL"],
                )
            )
            await s.commit()
            return str(claim.claim_id)

    async def test_reject(self, db):
        cid = await self._seed_manual_review(db)
        async with db() as s:
            row = await claims_service.resolve_claim(
                s, db, _settings(), FakeTransport(),
                claim_id=cid, decision="REJECT",
                reason_code="EVIDENCE_CONTRADICTS", reviewer="rev_1",
            )
            await s.commit()
        assert row.state == ClaimState.REJECTED.value

    async def test_reject_without_reason_works(self, db):
        cid = await self._seed_manual_review(db, internal="ord_cl41")
        async with db() as s:
            row = await claims_service.resolve_claim(
                s, db, _settings(), FakeTransport(),
                claim_id=cid, decision="REJECT", reviewer="rev_1",
            )
            await s.commit()
        assert row.state == ClaimState.REJECTED.value

    async def test_refund_decision_executes_money(self, db):
        cid = await self._seed_manual_review(db, internal="ord_cl42")
        t = FakeTransport().on("/refund", {"id": "RF-5", "status": "COMPLETED"})
        async with db() as s:
            row = await claims_service.resolve_claim(
                s, db, _settings(), t,
                claim_id=cid, decision="REFUND", reason_code="OK", reviewer="rev_2",
            )
        assert row.state == ClaimState.REFUNDED.value
        async with db() as s:
            rep = await s.get(MerchantReputationRow, "mer_cl")
            assert rep.verified_fraud_claims == 1

    async def test_unknown_claim(self, db):
        async with db() as s:
            with pytest.raises(ClaimNotFoundError):
                await claims_service.resolve_claim(
                    s, db, _settings(), FakeTransport(),
                    claim_id=str(uuid.uuid4()), decision="REJECT",
                )

    async def test_illegal_from_submitted(self, db):
        await _seed_captured_order(db, internal="ord_cl43")
        async with db() as s:
            claim = await submit_claim(s, _settings(), order_ref="PAY-ord_cl43", photo_bytes=_JPEG)
            await s.commit()
            with pytest.raises(IllegalClaimTransition):
                await claims_service.resolve_claim(
                    s, db, _settings(), FakeTransport(),
                    claim_id=str(claim.claim_id), decision="REFUND",
                )

    async def test_replay_after_refunded_is_safe(self, db):
        cid = await self._seed_manual_review(db, internal="ord_cl44")
        t = FakeTransport().on("/refund", {"id": "RF-6", "status": "COMPLETED"})
        async with db() as s:
            await claims_service.resolve_claim(
                s, db, _settings(), t, claim_id=cid, decision="REFUND", reviewer="rev_3",
            )
        async with db() as s:
            again = await claims_service.resolve_claim(
                s, db, _settings(), t, claim_id=cid, decision="REFUND", reviewer="rev_3",
            )
        assert again.state == ClaimState.REFUNDED.value
        assert len(t.calls) == 1


# ---------------------------------------------------------------------------
# Read model
# ---------------------------------------------------------------------------


class TestGetClaimRecord:
    async def test_manual_review_record_with_decision(self, db):
        await _seed_captured_order(db, internal="ord_cl50")
        async with db() as s:
            claim = await submit_claim(s, _settings(), order_ref="PAY-ord_cl50", photo_bytes=_JPEG)
            claim.state = ClaimState.MANUAL_REVIEW.value
            claim.discrepancy = Decimal("0.5000")
            s.add(
                ClaimEventRow(
                    claim_id=claim.claim_id, from_state="EVIDENCE_CHECKED",
                    to_state="MANUAL_REVIEW", actor="system",
                    reason_codes=["STUB_ARBITER_MANUAL"],
                )
            )
            await s.commit()
        async with db() as s:
            rec = await claims_service.get_claim_record(s, str(claim.claim_id))
            assert isinstance(rec, ClaimRecord)
            assert rec.state == ClaimState.MANUAL_REVIEW
            assert rec.claim_amount == Decimal("120.00")
            assert rec.currency_code == "USD"
            assert rec.refund_id is None
            assert rec.decision is not None
            assert rec.decision.reason_codes == ["STUB_ARBITER_MANUAL"]
            assert rec.decision.gate_discrepancy_ok is True

    async def test_unknown_claim_404_shape(self, db):
        async with db() as s:
            with pytest.raises(ClaimNotFoundError):
                await claims_service.get_claim_record(s, str(uuid.uuid4()))

    async def test_refunded_record_carries_refund_id(self, db):
        await _seed_captured_order(db, internal="ord_cl51")
        async with db() as s:
            claim = await submit_claim(s, _settings(), order_ref="PAY-ord_cl51", photo_bytes=_JPEG)
            s.add(
                RefundRow(
                    refund_id="RF-X1", claim_id=claim.claim_id,
                    amount=Decimal("120.00"), currency_code="USD", status="COMPLETED",
                )
            )
            claim.state = ClaimState.REFUNDED.value
            await s.commit()
        async with db() as s:
            rec = await claims_service.get_claim_record(s, str(claim.claim_id))
            assert rec.refund_id == "RF-X1"
            assert rec.state == ClaimState.REFUNDED

    async def test_invalid_claim_uuid_is_not_found_not_500(self, db):
        async with db() as s:
            with pytest.raises(ClaimNotFoundError):
                await claims_service.get_claim_record(s, "not-a-uuid")


# ---------------------------------------------------------------------------
# API level: multipart router + reviewer auth (demo/stub app, mirrors client.ts)
# ---------------------------------------------------------------------------

QUOTE_BODY = {
    "merchant_id": "mer_claim_e2e",
    "item_hash": "b" * 64,
    "item_title": "Claimed Gadget",
    "item_price": "80.00",
    "currency_code": "USD",
}


def _e2e_client(tmp_path) -> TestClient:
    settings = Settings(
        _env_file=None,
        engine_impl="stub",
        demo_deterministic_mode=True,
        paypal_client_id="",
        paypal_client_secret="",
        sqlite_url=f"sqlite+aiosqlite:///{tmp_path}/claims.db",
        quotes_hmac_key="claim-e2e-key-0123456789abcdef",
        claim_reviewer_token="rev-secret-token",
    )
    return TestClient(create_app_from(settings))


def create_app_from(settings: Settings):
    from app.main import create_app

    return create_app(settings)


def _capture_one(client: TestClient) -> str:
    """Quote -> order -> capture via the demo chain; returns PayPal order id."""
    r = client.post("/api/v1/quotes", json=QUOTE_BODY)
    assert r.status_code == 200
    quote = r.json()["quote"]
    r = client.post(
        "/api/v1/orders",
        json={"quote_id": quote["quote_id"], "buyer_ref": "buyer_e2e", "opt_in": True},
    )
    assert r.status_code == 201
    order_id = r.json()["order_id"]
    r = client.post(f"/api/v1/orders/{order_id}/capture")
    assert r.status_code == 200
    return order_id


class TestClaimsApiE2E:
    def test_submit_get_and_reviewer_auth(self, tmp_path):
        with _e2e_client(tmp_path) as client:
            order_id = _capture_one(client)

            # multipart submission exactly as the frontend sends it
            r = client.post(
                "/api/v1/claims",
                data={"order_id": order_id},
                files={"photo": ("receipt.jpg", _JPEG, "image/jpeg")},
            )
            assert r.status_code == 201
            rec = r.json()
            claim_id = rec["claim_id"]
            assert rec["state"] in {ClaimState.SUBMITTED.value, ClaimState.MANUAL_REVIEW.value}
            assert rec["claim_amount"] == "80.00"
            assert rec["currency_code"] == "USD"

            # duplicate submission replays the same claim (200-shape, no double)
            r2 = client.post(
                "/api/v1/claims",
                data={"order_ref": order_id},
                # Replays return the original record before inspecting a
                # replacement upload; idempotency does not re-run evidence.
                files={"photo": ("replacement.txt", b"not-an-image", "text/plain")},
            )
            assert r2.status_code == 200
            assert r2.json()["claim_id"] == claim_id

            # background task (stub engine) settles to MANUAL_REVIEW
            r = client.get(f"/api/v1/claims/{claim_id}")
            assert r.status_code == 200
            assert r.json()["state"] == ClaimState.MANUAL_REVIEW.value
            assert r.json()["decision"]["reason_codes"] == ["STUB_ARBITER_MANUAL"]

            # reviewer auth: no token -> 401
            r = client.post(
                f"/api/v1/claims/{claim_id}/resolve",
                json={"decision": "REJECT", "reason_code": "NO_TOKEN"},
            )
            assert r.status_code == 401
            # wrong token -> 401
            r = client.post(
                f"/api/v1/claims/{claim_id}/resolve",
                json={"decision": "REJECT", "reason_code": "WRONG"},
                headers={"X-Reviewer-Token": "not-it"},
            )
            assert r.status_code == 401
            # correct token -> REJECTED
            r = client.post(
                f"/api/v1/claims/{claim_id}/resolve",
                json={"decision": "REJECT", "reason_code": "NOT_GENUINE"},
                headers={"X-Reviewer-Token": "rev-secret-token"},
            )
            assert r.status_code == 200
            assert r.json()["state"] == ClaimState.REJECTED.value

    def test_submit_unknown_order_maps_to_404(self, tmp_path):
        with _e2e_client(tmp_path) as client:
            r = client.post(
                "/api/v1/claims",
                data={"order_id": "PAY-unknown"},
                files={"photo": ("r.jpg", _JPEG, "image/jpeg")},
            )
            assert r.status_code == 404

            r = client.get("/api/v1/claims/not-a-uuid")
            assert r.status_code == 404

    def test_submit_bad_photo_maps_to_400_invalid_upload(self, tmp_path):
        with _e2e_client(tmp_path) as client:
            order_id = _capture_one(client)
            r = client.post(
                "/api/v1/claims",
                data={"order_id": order_id},
                files={"photo": ("r.txt", b"not an image", "text/plain")},
            )
            assert r.status_code == 400

    def test_nonfinite_claim_amount_maps_to_400(self, tmp_path):
        with _e2e_client(tmp_path) as client:
            order_id = _capture_one(client)
            r = client.post(
                "/api/v1/claims",
                data={"order_id": order_id, "claim_amount": "NaN"},
                files={"photo": ("r.jpg", _JPEG, "image/jpeg")},
            )
            assert r.status_code == 400
            assert r.json()["error"]["code"] == "INVALID_CLAIM_AMOUNT"

    def test_demo_reviewer_refund_returns_fresh_refunded_record(self, tmp_path):
        with _e2e_client(tmp_path) as client:
            order_id = _capture_one(client)
            submitted = client.post(
                "/api/v1/claims",
                data={"order_id": order_id},
                files={"photo": ("r.jpg", _JPEG, "image/jpeg")},
            )
            assert submitted.status_code == 201
            claim_id = submitted.json()["claim_id"]

            resolved = client.post(
                f"/api/v1/claims/{claim_id}/resolve",
                json={"decision": "REFUND", "reason_code": "REVIEW_APPROVED"},
                headers={"X-Reviewer-Token": "rev-secret-token"},
            )
            assert resolved.status_code == 200
            body = resolved.json()
            assert body["state"] == ClaimState.REFUNDED.value
            assert body["refund_id"].startswith("DEMOREF")
            # The stored arbiter decision remains MANUAL_REVIEW even though
            # the claim's current state progressed to REFUNDED.
            assert body["decision"]["outcome"] == ClaimState.MANUAL_REVIEW.value

    def test_resolve_body_requires_contract_enum(self, tmp_path):
        with _e2e_client(tmp_path) as client:
            r = client.post(
                f"/api/v1/claims/{uuid.uuid4()}/resolve",
                json={"decision": "PAY"},
                headers={"X-Reviewer-Token": "rev-secret-token"},
            )
            assert r.status_code == 422

    def test_webhook_endpoint_demo_mode_accepts_and_dedupes(self, tmp_path):
        with _e2e_client(tmp_path) as client:
            headers = {"PAYPAL-TRANSMISSION-ID": "TXN-E2E-1"}
            body = {"id": "WH-E2E-1", "event_type": "CHECKOUT.ORDER.APPROVED"}
            r1 = client.post(
                "/api/v1/webhooks/paypal", json=body, headers=headers,
            )
            assert r1.status_code == 200
            assert r1.json()["status"] == "ACCEPTED"
            r2 = client.post(
                "/api/v1/webhooks/paypal", json=body, headers=headers,
            )
            assert r2.status_code == 200
            assert r2.json()["status"] == "DUPLICATE"


def json_body_dump(body: dict) -> str:
    import json as _json

    return _json.dumps(body).lower()


async def _noop():  # keeps async fixtures importable in some runners
    return None
