"""Claim workflow (Person 1, spec sections 3.4 / 6.6 / 6.7 / 2.7).

State machine (every transition writes an immutable claim_events row):

    SUBMITTED -> EVIDENCE_CHECKED -> AUTO_APPROVED | MANUAL_REVIEW | REJECTED
    AUTO_APPROVED -> REFUNDED          (refund COMPLETED, direct or webhook)
    MANUAL_REVIEW -> REFUNDED | REJECTED   (authorized reviewer)

Design rules enforced here:
- One claim per order; a repeat submission replays the stored claim
  (refunds.claim_id uniqueness is the second line of defense).
- Restart-safe: the pipeline always re-reads state from the DB and skips
  already-done steps, so a crash just resumes.
- Money safety: refund amount = min(claimed price, captured item price);
  the Buyer Protection Fee is never refunded (platform keeps the plan fee).
- Engine boundary: arbitration goes through engine_provider (stub fallback
  on failure, REQ-NF-03). The stub never auto-approves, so an unavailable
  listing-image source can only cause safe rejections/manual review.
- Closed loop (spec 6.7): refund COMPLETED -> refunds row + REFUND ledger
  entry + verified_fraud_claims+1 in one atomic SQL UPDATE (idempotent).
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import logging
import uuid
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Optional

from sqlalchemy import func, select, update as sa_update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.models import (
    ClaimEventRow,
    ClaimRow,
    LedgerEntryRow,
    MerchantReputationRow,
    OrderRow,
    RefundRow,
)
from app.schemas.contracts import (
    ArbiterContext,
    ArbiterDecision,
    ClaimRecord,
    ClaimState,
)
from app.services.ledger import post_capture_entries
from app.services.engine_provider import fallback_engine, get_risk_engine
from app.services.events import commit_and_emit, enqueue_event
from app.services.ledger import (
    merchant_trust_row_by_id,
    transaction_row_for_order,
)
from app.services.paypal.auth import PayPalAuthError
from app.services.paypal.orders import request_id_for_refund as _rid
from app.services.paypal.refunds import (
    PayPalClientError,
    PayPalTransientError,
    extract_refund_id,
    extract_refund_status,
    refund_capture,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Claim state machine (spec 3.4)
# ---------------------------------------------------------------------------
ALLOWED_CLAIM_TRANSITIONS: dict[str, frozenset[str]] = {
    "SUBMITTED": frozenset({"EVIDENCE_CHECKED"}),
    "EVIDENCE_CHECKED": frozenset({"AUTO_APPROVED", "MANUAL_REVIEW", "REJECTED"}),
    "AUTO_APPROVED": frozenset({"REFUNDED"}),
    "MANUAL_REVIEW": frozenset({"REFUNDED", "REJECTED"}),
    "REJECTED": frozenset(),
    "REFUNDED": frozenset(),
}


class ClaimError(Exception):
    """Base class for claim flow errors (handlers map to safe 4xx)."""


class ClaimNotFoundError(ClaimError):
    pass


class ClaimWindowError(ClaimError):
    """Claim outside the configured window since capture."""


class ClaimNotCapturedError(ClaimError):
    """Order tied to the claim is not CAPTURED."""


class ClaimAmountError(ClaimError):
    """claim_amount missing/zero/above the captured item price."""


class IllegalClaimTransition(ClaimError):
    def __init__(self, from_state: str, to_state: str) -> None:
        super().__init__(f"illegal claim transition: {from_state} -> {to_state}")
        self.from_state = from_state
        self.to_state = to_state


class ClaimUploadError(ClaimError):
    """Uploaded photo failed content-type / size / magic-bytes checks."""


def assert_claim_transition(from_state: str, to_state: str) -> None:
    if to_state not in ALLOWED_CLAIM_TRANSITIONS.get(from_state, frozenset()):
        raise IllegalClaimTransition(from_state, to_state)


# ---------------------------------------------------------------------------
# Upload safety (spec 6.8: validate by magic bytes, not declared type)
# ---------------------------------------------------------------------------
_MAGIC = (
    (b"\xff\xd8\xff", "jpeg"),
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"RIFF", "webp"),  # WEBP confirmed at offset 8 below
)


def validate_photo(data: bytes, max_bytes: int) -> str:
    """Return the detected image kind or raise ClaimUploadError."""
    if not data:
        raise ClaimUploadError("empty upload")
    if len(data) > max_bytes > 0:
        raise ClaimUploadError(f"upload exceeds {max_bytes} bytes")
    for magic, kind in _MAGIC:
        if data.startswith(magic):
            if kind == "webp" and data[8:12] != b"WEBP":
                continue
            return kind
    raise ClaimUploadError("upload is not jpeg/png/webp (magic bytes)")


def _now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def _aware(dt: _dt.datetime) -> _dt.datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=_dt.timezone.utc)
    return dt


def _claim_key(claim_id: str) -> uuid.UUID:
    """Parse an external claim id without leaking UUID binder errors as 500s."""
    try:
        return uuid.UUID(str(claim_id))
    except (TypeError, ValueError, AttributeError):
        raise ClaimNotFoundError(f"unknown claim {claim_id}") from None


def _image_hash_of(data: bytes) -> str:
    """SHA-256 digest of the received photo (raw evidence is not persisted)."""
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# Submission
# ---------------------------------------------------------------------------
async def find_order_flexible(session: AsyncSession, order_ref: str) -> Optional[OrderRow]:
    """Accept a PayPal order id or an internal order id."""
    # ``internal_order_id`` is Uuid(as_uuid=True). Passing an arbitrary PayPal
    # id to ``session.get`` makes SQLAlchemy's UUID binder access ``.hex`` on
    # the string before we ever reach the PayPal-id fallback query.
    try:
        internal_id = uuid.UUID(order_ref)
    except (TypeError, ValueError, AttributeError):
        internal_id = None

    row = await session.get(OrderRow, internal_id) if internal_id is not None else None
    if row is None:
        row = (
            await session.execute(
                select(OrderRow).where(OrderRow.paypal_order_id == order_ref)
            )
        ).scalar_one_or_none()
    return row


async def _capture_time(session: AsyncSession, order: OrderRow) -> _dt.datetime:
    """Use the capture ledger timestamp; fall back for legacy/dev rows."""
    captured_at = (
        await session.execute(
            select(LedgerEntryRow.at)
            .where(
                LedgerEntryRow.internal_order_id == order.internal_order_id,
                LedgerEntryRow.kind == "MERCHANT_REVENUE",
            )
            .order_by(LedgerEntryRow.at.asc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return _aware(captured_at or order.created_at)


async def submit_claim(
    session: AsyncSession,
    settings: Settings,
    *,
    order_ref: str,
    photo_bytes: bytes,
    claim_amount: Optional[Decimal] = None,
) -> ClaimRow:
    """Create (or replay) a claim for a CAPTURED order. Returns the ClaimRow.

    Eligibility (spec 3.4): order CAPTURED with a capture id, within the
    claim window, amount in (0, captured item price].
    """
    order = await find_order_flexible(session, order_ref)
    if order is None:
        raise ClaimNotFoundError(f"unknown order {order_ref}")

    existing = (
        await session.execute(
            select(ClaimRow).where(ClaimRow.internal_order_id == order.internal_order_id)
        )
    ).scalar_one_or_none()
    if existing is not None:
        logger.info("claim replay for order %s (state=%s)", order_ref, existing.state)
        return existing

    # A valid replay returns before inspecting a replacement upload; for a new
    # claim, validate the actual bytes rather than trusting Content-Type.
    kind = validate_photo(photo_bytes, settings.claim_photo_max_bytes)

    if order.state != "CAPTURED" or not order.paypal_capture_id:
        raise ClaimNotCapturedError(
            f"order {order_ref} is {order.state}, not eligible for a claim"
        )

    captured_at = await _capture_time(session, order)
    window_days = settings.claim_window_days
    if _now() - captured_at > _dt.timedelta(days=window_days):
        raise ClaimWindowError(
            f"claim window of {window_days} days since capture has passed"
        )

    item_price = Decimal(str(order.item_price))
    amount = item_price if claim_amount is None else Decimal(str(claim_amount))
    if not amount.is_finite() or amount <= 0 or amount > item_price:
        raise ClaimAmountError(
            f"claim amount {amount} must be within (0, captured item price {item_price}]"
        )
    quantum = (
        Decimal(1)
        if order.currency_code in ("JPY", "HUF", "TWD")
        else Decimal("0.01")
    )
    try:
        amount = amount.quantize(quantum, rounding=ROUND_HALF_UP)
    except InvalidOperation:
        raise ClaimAmountError("claim amount cannot be represented safely") from None
    if amount <= 0:
        raise ClaimAmountError("claim amount rounds to zero in the order currency")

    claim = ClaimRow(
        claim_id=uuid.uuid4(),
        internal_order_id=order.internal_order_id,
        state=ClaimState.SUBMITTED.value,
        claim_amount=amount,
        currency_code=order.currency_code,
        received_image_hash=_image_hash_of(photo_bytes),
    )
    try:
        # A savepoint makes a concurrent unique(internal_order_id) collision a
        # replay instead of poisoning the request transaction.
        async with session.begin_nested():
            session.add(claim)
            await session.flush()
            await _event(
                session, claim, from_state=None,
                to_state=ClaimState.SUBMITTED.value, actor="system",
                reason_codes=["CLAIM_SUBMITTED", f"PHOTO_{kind.upper()}"],
            )
    except IntegrityError:
        existing = (
            await session.execute(
                select(ClaimRow).where(
                    ClaimRow.internal_order_id == order.internal_order_id
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            logger.info("concurrent claim replay for order %s", order_ref)
            return existing
        raise

    # Stream: claim add for a NEW claim only. Replays return earlier, and the
    # record is re-built post-commit (factory), so the feed never duplicates
    # or leads the DB.
    enqueue_event(
        session, "claim", "add",
        lambda s: get_claim_record(s, str(claim.claim_id)),
    )
    return claim


# ---------------------------------------------------------------------------
# Immutable audit trail
# ---------------------------------------------------------------------------
async def _event(
    session: AsyncSession,
    claim: ClaimRow,
    *,
    from_state: Optional[str],
    to_state: str,
    actor: str,
    reason_codes: list[str],
) -> None:
    """Append one immutable claim_events row (never updated, per spec)."""
    session.add(
        ClaimEventRow(
            claim_id=claim.claim_id,
            from_state=from_state,
            to_state=to_state,
            actor=actor,
            reason_codes=list(reason_codes),
        )
    )
    await session.flush()


# ---------------------------------------------------------------------------
# Evidence + arbitration pipeline (background; restart-resumable)
# ---------------------------------------------------------------------------
def _listing_placeholder_bytes(item_hash: str) -> bytes:
    """Deterministic stand-in for the (unstored) listing image bytes.

    KNOWN GAP (team decision needed): the spec assumes a listing-image
    source exists server-side. Failure direction is safe: a buyer who
    reproduces these exact bytes gets discrepancy ~0 -> REJECTED; the stub
    engine never auto-approves at all.
    """
    seed = f"listing-image:{item_hash}".encode("utf-8")
    return (seed * 128)[:4096]


def _received_placeholder_bytes(claim: ClaimRow) -> bytes:
    """Same rationale as the listing placeholder (real deployments relay the
    uploaded photo through the background task instead of a digest)."""
    return f"received-image:{claim.received_image_hash}".encode("utf-8")


async def _buyer_velocity(
    session: AsyncSession,
    settings: Settings,
    claim: ClaimRow,
    order: OrderRow,
) -> tuple[int, Decimal]:
    """Gate 3 inputs: claims in the trailing window + claims/orders ratio."""
    since = _now() - _dt.timedelta(days=settings.claim_velocity_window_days)
    past = (
        await session.execute(
            select(func.count())
            .select_from(ClaimRow)
            .join(OrderRow, ClaimRow.internal_order_id == OrderRow.internal_order_id)
            .where(
                OrderRow.buyer_ref == order.buyer_ref,
                ClaimRow.created_at >= since,
                ClaimRow.claim_id != claim.claim_id,
            )
        )
    ).scalar_one()
    orders_count = (
        await session.execute(
            select(func.count())
            .select_from(OrderRow)
            .where(
                OrderRow.buyer_ref == order.buyer_ref,
                OrderRow.created_at >= since,
                OrderRow.state.in_(
                    ("CAPTURED", "PARTIALLY_REFUNDED", "REFUNDED")
                ),
            )
        )
    ).scalar_one() or 1
    return int(past), Decimal(past) / Decimal(orders_count)


async def _arbitrate(
    session: AsyncSession,
    settings: Settings,
    claim: ClaimRow,
    order: OrderRow,
    risk_engine=None,
) -> ArbiterDecision:
    """Engine arbitration with stub fallback (REQ-NF-03 never 500s a claim)."""
    past_claims, ratio = await _buyer_velocity(session, settings, claim, order)
    claim_record = ClaimRecord(
        claim_id=str(claim.claim_id),
        order_id=str(order.internal_order_id),
        state=ClaimState(claim.state),
        claim_amount=Decimal(str(claim.claim_amount)),
        currency_code=claim.currency_code,
        updated_at=_now(),
    )
    ctx = ArbiterContext(
        claim_amount=Decimal(str(claim.claim_amount)),
        currency_code=claim.currency_code,
        buyer_past_claims=past_claims,
        buyer_claim_ratio=ratio,
        dispute_active=bool(order.dispute_active),
    )
    listing = _listing_placeholder_bytes(order.quote_id)
    received = _received_placeholder_bytes(claim)
    try:
        engine = risk_engine if risk_engine is not None else get_risk_engine()
        decision = await engine.arbitrate(claim_record, listing, received, ctx)
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "engine.arbitrate failed (%s: %s); stub fallback", type(exc).__name__, exc
        )
        decision = await fallback_engine().arbitrate(claim_record, listing, received, ctx)

    # Person 2's arbiter owns its calibrated defaults. Person 1's configured
    # gates are an additional payout-safety ceiling: they may downgrade an
    # approval to MANUAL_REVIEW, never create an approval.
    amount_ok = bool(
        decision.gate_amount_ok
        and Decimal(str(claim.claim_amount)) <= settings.claim_auto_payout_cap
    )
    velocity_ok = bool(
        decision.gate_velocity_ok
        and past_claims < settings.claim_velocity_max_claims
        and ratio < settings.claim_velocity_max_ratio
    )
    no_dispute = bool(decision.gate_no_dispute and not order.dispute_active)
    reasons = list(decision.reason_codes)
    if not amount_ok and "AMOUNT_EXCEEDS_LIMIT" not in reasons:
        reasons.append("AMOUNT_EXCEEDS_LIMIT")
    if not velocity_ok and "BUYER_VELOCITY_EXCEEDED" not in reasons:
        reasons.append("BUYER_VELOCITY_EXCEEDED")
    if not no_dispute and "ACTIVE_DISPUTE_PRESENT" not in reasons:
        reasons.append("ACTIVE_DISPUTE_PRESENT")
    outcome = decision.outcome
    if outcome == ClaimState.AUTO_APPROVED and not (
        decision.gate_discrepancy_ok and amount_ok and velocity_ok and no_dispute
    ):
        outcome = ClaimState.MANUAL_REVIEW
        reasons = [r for r in reasons if r != "ALL_GATES_PASSED"]
    return decision.model_copy(
        update={
            "gate_amount_ok": amount_ok,
            "gate_velocity_ok": velocity_ok,
            "gate_no_dispute": no_dispute,
            "outcome": outcome,
            "reason_codes": reasons,
        }
    )


async def run_claim_pipeline(
    session_factory,
    settings: Settings,
    transport,
    *,
    claim_id: str,
    risk_engine=None,
) -> None:
    """Evidence -> arbitrate -> auto-refund. Safe to run repeatedly."""
    try:
        key = _claim_key(claim_id)
    except ClaimNotFoundError:
        logger.error("pipeline: invalid claim id %s", claim_id)
        return

    should_refund = False
    async with session_factory() as session:
        claim = (
            await session.execute(
                select(ClaimRow)
                .where(ClaimRow.claim_id == key)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if claim is None:
            logger.error("pipeline: claim %s vanished", claim_id)
            return
        if claim.state == ClaimState.AUTO_APPROVED.value:
            # A previous worker may have committed the decision and crashed
            # before the PayPal call. Resume with the same deterministic id.
            should_refund = True
        elif claim.state != ClaimState.SUBMITTED.value:
            logger.info("pipeline: claim %s already %s; skip", claim_id, claim.state)
            return
        else:
            order = await session.get(OrderRow, claim.internal_order_id)
            if order is None:  # pragma: no cover - FK guarantees presence
                return

            # ---- evidence check --------------------------------------------
            assert_claim_transition(claim.state, ClaimState.EVIDENCE_CHECKED.value)
            claim.state = ClaimState.EVIDENCE_CHECKED.value
            await _event(
                session, claim, from_state=ClaimState.SUBMITTED.value,
                to_state=ClaimState.EVIDENCE_CHECKED.value, actor="system",
                reason_codes=["EVIDENCE_PHOTO_DECODED"],
            )

            # ---- arbitrate -------------------------------------------------
            decision = await _arbitrate(
                session,
                settings,
                claim,
                order,
                risk_engine=risk_engine,
            )
            claim.discrepancy = decision.discrepancy_score
            assert_claim_transition(claim.state, decision.outcome.value)
            claim.state = decision.outcome.value
            await _event(
                session, claim, from_state=ClaimState.EVIDENCE_CHECKED.value,
                to_state=decision.outcome.value, actor="system",
                reason_codes=list(decision.reason_codes),
            )
            enqueue_event(
                session, "claim", "update",
                lambda s: get_claim_record(s, claim_id),
            )
            await commit_and_emit(session)
            should_refund = decision.outcome == ClaimState.AUTO_APPROVED

    if should_refund:
        await execute_refund(
            session_factory, settings, transport,
            claim_id=claim_id, actor="system",
        )


# ---------------------------------------------------------------------------
# Refund execution + closed loop (spec 2.7 / 6.7)
# ---------------------------------------------------------------------------
async def execute_refund(
    session_factory,
    settings: Settings,
    transport,
    *,
    claim_id: str,
    actor: str = "system",
) -> ClaimRow:
    """Refund the claimed item price via PayPal. Idempotent + restart-safe.

    Legal from AUTO_APPROVED (auto path) or MANUAL_REVIEW (reviewer path).
    Outcome mapping (spec 2.8 payout safety):
      COMPLETED -> REFUNDED (+ closed loop)
      PENDING   -> stays AUTO_APPROVED, awaiting webhook confirmation
      client error -> MANUAL_REVIEW with REFUND_ISSUE_<code>
      transient -> stays, REFUND_TRANSIENT_RETRY_LATER
    """
    async with session_factory() as session:
        claim = (
            await session.execute(
                select(ClaimRow)
                .where(ClaimRow.claim_id == _claim_key(claim_id))
                .with_for_update()
            )
        ).scalar_one_or_none()
        if claim is None:
            raise ClaimNotFoundError(f"unknown claim {claim_id}")
        if claim.state == ClaimState.REFUNDED.value:
            return claim  # replay
        if claim.state not in (
            ClaimState.AUTO_APPROVED.value, ClaimState.MANUAL_REVIEW.value
        ):
            raise IllegalClaimTransition(claim.state, ClaimState.REFUNDED.value)

        order = await session.get(OrderRow, claim.internal_order_id)
        if order is None or not order.paypal_capture_id:  # defensive
            prev = claim.state
            claim.state = ClaimState.MANUAL_REVIEW.value
            await _event(
                session, claim, from_state=prev,
                to_state=ClaimState.MANUAL_REVIEW.value, actor=actor,
                reason_codes=["REFUND_BLOCKED_NO_CAPTURE_ID"],
            )
            enqueue_event(
                session, "claim", "update",
                lambda s: get_claim_record(s, claim_id),
            )
            await commit_and_emit(session)
            return claim

        # Local idempotency line: refunds.claim_id unique.
        existing = (
            await session.execute(
                select(RefundRow).where(RefundRow.claim_id == claim.claim_id)
            )
        ).scalar_one_or_none()
        if existing is not None and existing.status == "COMPLETED":
            logger.info("refund for claim %s already COMPLETED; replay", claim_id)
            return await _finalize_refunded(session, claim, order, existing, actor)

        try:
            response = await refund_capture(
                transport,
                capture_id=str(order.paypal_capture_id),
                claim_id=str(claim.claim_id),
                item_price=Decimal(str(order.item_price)),
                claim_amount=Decimal(str(claim.claim_amount)),
                currency_code=claim.currency_code,
            )
            status = extract_refund_status(response).upper()
            refund_id = extract_refund_id(response)
        except PayPalClientError as exc:
            issue = exc.issues[0] if exc.issues else "REFUND_CLIENT_ERROR"
            logger.warning("refund rejected claim=%s issue=%s", claim_id, issue)
            prev = claim.state
            claim.state = ClaimState.MANUAL_REVIEW.value
            await _event(
                session, claim, from_state=prev,
                to_state=ClaimState.MANUAL_REVIEW.value, actor=actor,
                reason_codes=[f"REFUND_ISSUE_{issue}", f"DEBUG_{exc.debug_id or 'n/a'}"],
            )
            enqueue_event(
                session, "claim", "update",
                lambda s: get_claim_record(s, claim_id),
            )
            await commit_and_emit(session)
            return claim
        except (PayPalTransientError, PayPalAuthError) as exc:
            logger.warning("refund transient exhaustion claim=%s (%s)", claim_id, exc)
            await _event(
                session, claim, from_state=claim.state,
                to_state=claim.state, actor=actor,
                reason_codes=["REFUND_TRANSIENT_RETRY_LATER"],
            )
            enqueue_event(
                session, "claim", "update",
                lambda s: get_claim_record(s, claim_id),
            )
            await commit_and_emit(session)
            return claim

        if existing is None:
            existing = RefundRow(
                refund_id=refund_id,
                claim_id=claim.claim_id,
                amount=Decimal(str(claim.claim_amount)),
                currency_code=claim.currency_code,
                status=status or "PENDING",
            )
            session.add(existing)
        else:
            existing.refund_id = refund_id
            existing.status = status or existing.status

        if status == "COMPLETED":
            return await _finalize_refunded(session, claim, order, existing, actor)

        # PENDING: await the PAYMENT.CAPTURE.REFUNDED webhook.
        await _event(
            session, claim, from_state=claim.state,
            to_state=claim.state, actor=actor,
            reason_codes=[
                f"REFUND_{status or 'PENDING'}_AWAITING_WEBHOOK",
                f"REFUND_ID_{refund_id}",
            ],
        )
        enqueue_event(
            session, "claim", "update",
            lambda s: get_claim_record(s, claim_id),
        )
        await commit_and_emit(session)
        return claim


async def _finalize_refunded(
    session: AsyncSession,
    claim: ClaimRow,
    order: OrderRow,
    refund_row: RefundRow,
    actor: str,
) -> ClaimRow:
    """One transaction, spec 6.7: state + ledger + atomic fraud counter."""
    if claim.state == ClaimState.REFUNDED.value:
        return claim

    prev = claim.state
    refund_amount = Decimal(str(refund_row.amount))

    claim.state = ClaimState.REFUNDED.value
    if refund_amount >= Decimal(str(order.item_price)):
        order.state = "REFUNDED"
    else:
        order.state = "PARTIALLY_REFUNDED"

    has_refund_entry = (
        await session.execute(
            select(LedgerEntryRow.id)
            .where(
                LedgerEntryRow.internal_order_id == order.internal_order_id,
                LedgerEntryRow.kind == "REFUND",
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if has_refund_entry is None:
        session.add(
            LedgerEntryRow(
                internal_order_id=order.internal_order_id,
                kind="REFUND",
                amount=refund_amount,
                currency_code=order.currency_code,
            )
        )

    # Closed loop (spec 6.7): atomic increment; Bayesian trust derives on read.
    await session.execute(
        sa_update(MerchantReputationRow)
        .where(MerchantReputationRow.merchant_id == order.merchant_id)
        .values(verified_fraud_claims=MerchantReputationRow.verified_fraud_claims + 1)
    )

    await _event(
        session, claim, from_state=prev,
        to_state=ClaimState.REFUNDED.value, actor=actor,
        reason_codes=[f"REFUND_COMPLETED_{refund_row.refund_id}"],
    )
    # Stream: the persisted closed-loop tells three contract topics at once —
    # claim -> REFUNDED, order transaction changed, merchant trust changed.
    enqueue_event(
        session, "claim", "update",
        lambda s: get_claim_record(s, str(claim.claim_id)),
    )
    enqueue_event(
        session, "transaction", "update",
        lambda s: transaction_row_for_order(s, order),
    )
    enqueue_event(
        session, "merchant", "update",
        lambda s: merchant_trust_row_by_id(s, order.merchant_id),
    )
    await commit_and_emit(session)
    logger.info(
        "claim %s REFUNDED (%s %s); merchant %s closed-loop penalty applied",
        claim.claim_id, refund_amount, order.currency_code, order.merchant_id,
    )
    return claim


# ---------------------------------------------------------------------------
# Webhook confirmations
# ---------------------------------------------------------------------------
async def confirm_refund_webhook(session: AsyncSession, resource: dict[str, Any]) -> str:
    """PAYMENT.CAPTURE.REFUNDED: correlate via custom_id (claim id) / refund id."""
    custom_id = resource.get("custom_id")
    refund_row: Optional[RefundRow] = None
    if custom_id:
        try:
            key = uuid.UUID(str(custom_id))
        except ValueError:
            key = None
        if key is not None:
            refund_row = (
                await session.execute(
                    select(RefundRow).where(RefundRow.claim_id == key)
                )
            ).scalar_one_or_none()
    if refund_row is None and resource.get("id"):
        refund_row = (
            await session.execute(
                select(RefundRow).where(
                    RefundRow.refund_id == str(resource["id"])
                )
            )
        ).scalar_one_or_none()
    if refund_row is None:
        logger.info("refund webhook matched no claim (custom_id=%s)", custom_id)
        return "unmatched"

    claim = (
        await session.execute(
            select(ClaimRow)
            .where(ClaimRow.claim_id == refund_row.claim_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if claim is None:  # pragma: no cover - FK guarantees
        return "unmatched"
    # Keep the same lock order as execute_refund (claim -> refund) to avoid a
    # PostgreSQL deadlock if the webhook races the direct API response.
    refund_row = (
        await session.execute(
            select(RefundRow)
            .where(RefundRow.refund_id == refund_row.refund_id)
            .with_for_update()
        )
    ).scalar_one()
    refund_row.status = "COMPLETED"
    if claim.state in (ClaimState.AUTO_APPROVED.value, ClaimState.MANUAL_REVIEW.value):
        order = await session.get(OrderRow, claim.internal_order_id)
        await _finalize_refunded(session, claim, order, refund_row, actor="webhook")
        return "refund_confirmed"
    logger.info("refund webhook for claim in state %s; no-op", claim.state)
    return "noop"


async def reconcile_capture_webhook(
    session: AsyncSession, order: OrderRow, resource: dict[str, Any]
) -> str:
    """PAYMENT.CAPTURE.COMPLETED: async capture confirmation.

    Fills a missing capture id and completes limbo orders (spec 3.3 step 5).
    Never downgrades CAPTURED.
    """
    capture_id = resource.get("id")
    changed = False
    if capture_id and order.paypal_capture_id is None:
        order.paypal_capture_id = str(capture_id)
        changed = True
    if order.state in ("ORDER_CREATED", "PENDING_RECONCILIATION", "CAPTURE_FAILED"):
        from app.services.orders_flow import assert_transition

        assert_transition(order.state, "CAPTURED")
        order.state = "CAPTURED"
        await post_capture_entries(session, order)  # idempotent
        changed = True
    else:
        logger.info("capture-completed for order in state %s; no-op", order.state)

    if changed:
        # Stream: transaction update, published when the webhook task's own
        # transaction commits (api.webhooks._process_event drains post-commit).
        enqueue_event(
            session, "transaction", "update",
            lambda s: transaction_row_for_order(s, order),
        )
    return "reconciled" if changed else "noop"


# ---------------------------------------------------------------------------
# Reviewer actions (admin endpoint, spec 3.4 MANUAL_REVIEW row)
# ---------------------------------------------------------------------------
async def resolve_claim(
    session: AsyncSession,
    session_factory,
    settings: Settings,
    transport,
    *,
    claim_id: str,
    decision: str,  # "REFUND" | "REJECT"
    reason_code: str = "",
    reviewer: str = "reviewer",
) -> ClaimRow:
    """MANUAL_REVIEW -> REFUNDED | REJECTED (actor = reviewer id)."""
    claim = (
        await session.execute(
            select(ClaimRow)
            .where(ClaimRow.claim_id == _claim_key(claim_id))
            .with_for_update()
        )
    ).scalar_one_or_none()
    if claim is None:
        raise ClaimNotFoundError(f"unknown claim {claim_id}")
    if decision not in ("REFUND", "REJECT"):
        raise IllegalClaimTransition(claim.state, f"INVALID_DECISION_{decision}")
    if claim.state == ClaimState.REFUNDED.value:
        return claim  # replay-safe
    if claim.state != ClaimState.MANUAL_REVIEW.value:
        raise IllegalClaimTransition(claim.state, decision)

    if decision == "REJECT":
        claim.state = ClaimState.REJECTED.value
        await _event(
            session, claim, from_state=ClaimState.MANUAL_REVIEW.value,
            to_state=ClaimState.REJECTED.value, actor=reviewer,
            reason_codes=[f"REVIEWER_REJECT_{reason_code or 'NO_REASON'}"],
        )
        enqueue_event(
            session, "claim", "update",
            lambda s: get_claim_record(s, claim_id),
        )
        await commit_and_emit(session)
        return claim

    # Release this session's row lock before the dedicated refund transaction
    # acquires it; otherwise two sessions would deadlock on PostgreSQL.
    await session.rollback()
    result = await execute_refund(
        session_factory, settings, transport,
        claim_id=claim_id, actor=reviewer,
    )
    session.expire_all()
    return result


# ---------------------------------------------------------------------------
# Read model (ClaimRecord for the API layer)
# ---------------------------------------------------------------------------
async def get_claim_record(session: AsyncSession, claim_id: str) -> ClaimRecord:
    claim = await session.get(ClaimRow, _claim_key(claim_id))
    if claim is None:
        raise ClaimNotFoundError(f"unknown claim {claim_id}")
    refund_row = (
        await session.execute(
            select(RefundRow).where(RefundRow.claim_id == claim.claim_id)
        )
    ).scalar_one_or_none()
    last_event = (
        await session.execute(
            select(ClaimEventRow)
            .where(ClaimEventRow.claim_id == claim.claim_id)
            .order_by(ClaimEventRow.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    arbiter_event = (
        await session.execute(
            select(ClaimEventRow)
            .where(
                ClaimEventRow.claim_id == claim.claim_id,
                ClaimEventRow.from_state == ClaimState.EVIDENCE_CHECKED.value,
            )
            .order_by(ClaimEventRow.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    updated_at = _now()
    if last_event is not None and last_event.at:
        updated_at = _aware(last_event.at)
    elif claim.created_at:
        updated_at = _aware(claim.created_at)

    decision = None
    if claim.discrepancy is not None:
        reasons = (
            list(arbiter_event.reason_codes)
            if arbiter_event is not None and arbiter_event.reason_codes
            else []
        )
        decision_outcome = (
            ClaimState(arbiter_event.to_state)
            if arbiter_event is not None
            else ClaimState(claim.state)
        )
        decision = ArbiterDecision(
            claim_id=str(claim.claim_id),
            discrepancy_score=Decimal(str(claim.discrepancy)),
            gate_amount_ok="AMOUNT_EXCEEDS_LIMIT" not in reasons,
            gate_velocity_ok="BUYER_VELOCITY_EXCEEDED" not in reasons,
            gate_no_dispute="ACTIVE_DISPUTE_PRESENT" not in reasons,
            gate_discrepancy_ok=not any(
                code in reasons for code in ("DISCREPANCY_MATCH", "DISCREPANCY_LOW")
            ),
            outcome=decision_outcome,
            reason_codes=reasons,
        )
    return ClaimRecord(
        claim_id=str(claim.claim_id),
        order_id=str(claim.internal_order_id),
        state=ClaimState(claim.state),
        claim_amount=Decimal(str(claim.claim_amount)),
        currency_code=claim.currency_code,
        decision=decision,
        refund_id=refund_row.refund_id if refund_row else None,  # type: ignore[union-attr]
        updated_at=updated_at,
    )


__all__ = [
    "submit_claim",
    "run_claim_pipeline",
    "execute_refund",
    "resolve_claim",
    "confirm_refund_webhook",
    "reconcile_capture_webhook",
    "get_claim_record",
    "validate_photo",
    "assert_claim_transition",
    "ALLOWED_CLAIM_TRANSITIONS",
    "ClaimError",
    "ClaimNotFoundError",
    "ClaimWindowError",
    "ClaimNotCapturedError",
    "ClaimAmountError",
    "ClaimUploadError",
    "IllegalClaimTransition",
    "_rid",
    "request_id_for_refund",
]


def request_id_for_refund(claim_id: str) -> str:
    """Re-exported deterministic refund request id (spec 2.7)."""
    return _rid(claim_id)
