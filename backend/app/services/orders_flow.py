"""Order lifecycle flow (Person 1, spec section 3.3 step 2-3).

create_order: quote (HMAC-verified, from the DB row) -> deterministic
internal_order_id (uuid5 of quote_id|buyer_ref, so double-clicks collapse) ->
strict-invariant PayPal payload -> Orders v2 create -> ORDER_CREATED.
The quote is marked consumed ONLY when the buyer opted in (a non-opt-in
order never charges the Buyer Protection Fee and leaves the quote reusable).

capture_order: re-verifies the quote from the DB row (signature, expiry and
money equality against the order row) -> capture -> CAPTURED + ledger +
reputation counters. Replays of an already-captured order return the stored
response. Transient exhaustion or ORDER_ALREADY_CAPTURED lands the order in
PENDING_RECONCILIATION (matching a later retry or human reconciliation).

Request-ids are derived deterministically in services.paypal.orders so every
retry ships the same PayPal-Request-Id.
"""

from __future__ import annotations

import logging
import uuid
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.contracts import (
    CaptureResponse,
    CreateOrderResponse,
    ProtectionQuote,
)
from app.services.ledger import post_capture_entries, transaction_row_for_order
from app.services.paypal.errors import (
    IllegalOrderTransition,
    OrderNotFoundError,
    PayPalClientError,
    PayPalTransientError,
)
from app.services.paypal.orders import (
    build_order_payload,
    capture_paypal_order,
    create_paypal_order,
    extract_capture_id,
    request_id_for_order,
    request_id_for_quote,
)
from app.services.quotes import (
    QuoteConsumedError,
    QuoteError,
    QuoteExpiredError,
    QuoteSignatureError,
    mark_consumed,
    verify_quote,
)
from app.db.models import OrderRow, QuoteRow
from app.services.events import commit_and_emit, enqueue_event

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Order state machine (spec section 3.3)
# ---------------------------------------------------------------------------
ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "QUOTED": frozenset({"ORDER_CREATED", "QUOTE_EXPIRED"}),
    "ORDER_CREATED": frozenset({"CAPTURED", "CAPTURE_FAILED", "PENDING_RECONCILIATION"}),
    # reconciliation is a retryable limbo: next capture may complete or fail again
    "PENDING_RECONCILIATION": frozenset({"CAPTURED", "CAPTURE_FAILED", "PENDING_RECONCILIATION"}),
    "CAPTURE_FAILED": frozenset({"CAPTURED", "PENDING_RECONCILIATION"}),
    "CAPTURED": frozenset({"PARTIALLY_REFUNDED", "REFUNDED"}),
    "PARTIALLY_REFUNDED": frozenset({"REFUNDED"}),
    "QUOTE_EXPIRED": frozenset(),
    "REFUNDED": frozenset(),
}


def assert_transition(from_state: str, to_state: str) -> None:
    if to_state not in ALLOWED_TRANSITIONS.get(from_state, frozenset()):
        raise IllegalOrderTransition(from_state, to_state)


def internal_order_id_for(quote_id: str, buyer_ref: str) -> str:
    """Deterministic dedupe key: the same (quote, buyer) is the same order."""
    return str(uuid.uuid5(REQUEST_NS, f"order:{quote_id}|{buyer_ref}"))


REQUEST_NS = uuid.uuid5(uuid.NAMESPACE_URL, "paypal-shield-app-v1")

# PayPal's capture-of-captured conflict surfaces as this v2 issue name.
ALREADY_CAPTURED_ISSUE = "ORDER_ALREADY_CAPTURED"

# Placeholder display name: spec's CreateOrderRequest carries no item title
# (known contract gap -> P4 decision). Derived from the listing hash, stable.
def _display_name(item_hash: str) -> str:
    return f"Listing {item_hash[:12]}"


def _quote_from_row(row: QuoteRow) -> ProtectionQuote:
    """Reconstruct the contract object so quotes.verify_quote can re-check it."""
    expires_at = row.expires_at
    if expires_at.tzinfo is None:
        from datetime import timezone

        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return ProtectionQuote(
        quote_id=row.quote_id,
        merchant_id=row.merchant_id,
        item_hash=row.item_hash,
        item_price=row.item_price,
        currency_code=row.currency_code,
        protection_fee=row.protection_fee,
        expires_at=expires_at,
        hmac_signature=row.hmac_signature,
    )


async def _load_quote_row(session: AsyncSession, quote_id: str) -> QuoteRow:
    """Raw quote row for verification (does NOT reject consumed - capture needs it)."""
    row = (
        await session.execute(select(QuoteRow).where(QuoteRow.quote_id == quote_id))
    ).scalar_one_or_none()
    if row is None:
        raise OrderNotFoundError(f"unknown quote {quote_id}")
    return row


def _verify_query_matches(quote: QuoteRow, order: OrderRow) -> None:
    """Money equality between quote and order row (defense against drift)."""
    if order.quote_id != quote.quote_id:
        raise QuoteSignatureError("order does not reference its own quote")
    if Decimal(str(quote.item_price)) != Decimal(str(order.item_price)):
        raise QuoteSignatureError("quote item price does not match the order")
    if Decimal(str(quote.protection_fee)) != Decimal(str(order.protection_fee)):
        raise QuoteSignatureError("quote protection fee does not match the order")
    if quote.currency_code != order.currency_code:
        raise QuoteSignatureError("quote currency does not match the order")


async def create_order(
    session: AsyncSession,
    transport,
    settings,
    *,
    quote_id: str,
    buyer_ref: str,
    opt_in: bool,
) -> CreateOrderResponse:
    """Create (or replay) the PayPal order for a signed quote.

    Idempotent by construction:
    - deterministic internal_order_id PK collapses identical retries;
    - deterministic PayPal-Request-Id makes PayPal-side replays safe;
    - consumed-flag flip happens only for opt_in orders, inside the same
      transaction that persists the order row.
    """
    internal_order_id = internal_order_id_for(quote_id, buyer_ref)

    # ---- replay path ------------------------------------------------------
    existing = await session.get(OrderRow, uuid.UUID(internal_order_id))
    if existing is not None:
        if existing.paypal_order_id is None:  # pragma: no cover - defensive
            raise OrderNotFoundError("stored order missing PayPal order id")
        fee = Decimal(str(existing.protection_fee))
        total = Decimal(str(existing.item_price)) + fee
        return CreateOrderResponse(
            order_id=existing.paypal_order_id,
            internal_order_id=str(existing.internal_order_id),
            status=existing.state,
            total=total,
            currency_code=existing.currency_code,
        )

    # ---- verify the quote from the DB row ---------------------------------
    quote_row = await _load_quote_row(session, quote_id)
    if quote_row.consumed:
        # Only opt-in orders flip the flag, so a consumed quote always means
        # an order already exists for it (idempotency defense line 1).
        raise QuoteConsumedError(f"quote {quote_id} already used for an order")
    try:
        verify_quote(_quote_from_row(quote_row), settings)
    except QuoteExpiredError:
        # Landed QUOTED -> QUOTE_EXPIRED for any order attempt on an expired quote.
        logger.info("quote %s expired at order creation", quote_id)
        raise
    except QuoteError:
        raise

    protection_fee: Optional[Decimal]
    if opt_in:
        protection_fee = Decimal(str(quote_row.protection_fee))
    else:
        protection_fee = None  # strictly no fee line for non-opt-in buyers

    payload = build_order_payload(
        paypal_reference_id=internal_order_id,
        item_price=Decimal(str(quote_row.item_price)),
        item_name=_display_name(quote_row.item_hash),
        currency_code=quote_row.currency_code,
        protection_fee=protection_fee,
    )

    # ---- PayPal call (deterministic request id) ---------------------------
    response = await create_paypal_order(
        transport,
        request_id=request_id_for_quote(quote_id),
        payload=payload,
    )
    paypal_order_id = str(response.get("id", ""))
    if not paypal_order_id:
        raise PayPalTransientError(
            "PayPal returned no order id",
            request_id=request_id_for_quote(quote_id),
            attempts=1,
        )

    # ---- persist + consumed flag atomically --------------------------------
    fee = protection_fee if protection_fee is not None else Decimal("0")
    order = OrderRow(
        internal_order_id=uuid.UUID(internal_order_id),
        quote_id=quote_id,
        paypal_order_id=paypal_order_id,
        merchant_id=quote_row.merchant_id,
        buyer_ref=buyer_ref,
        opted_in=bool(opt_in),
        item_price=Decimal(str(quote_row.item_price)),
        protection_fee=fee,
        currency_code=quote_row.currency_code,
        state="ORDER_CREATED",
    )
    session.add(order)
    if opt_in:
        # One-way flip; a racing duplicate simply finds the row untouched->False
        # result, but the unique internal_order_id already collapsed the replay.
        consumed_flipped = await mark_consumed(session, quote_id)
        if not consumed_flipped:  # pragma: no cover - race guard
            logger.warning("quote %s consumed flag raced at order creation", quote_id)
    await session.flush()

    # Stream: transaction add for a NEW order only. The replay path returned
    # earlier, so retried double-clicks never emit a duplicate add event, and
    # the record is built post-commit so no unpublished state can leak.
    enqueue_event(
        session, "transaction", "add",
        lambda s: transaction_row_for_order(s, order),
    )

    total = Decimal(str(quote_row.item_price)) + fee
    return CreateOrderResponse(
        order_id=paypal_order_id,
        internal_order_id=internal_order_id,
        status="ORDER_CREATED",
        total=total,
        currency_code=quote_row.currency_code,
    )


async def capture_order(
    session: AsyncSession,
    transport,
    settings,
    *,
    paypal_order_id: str,
) -> CaptureResponse:
    """Capture (or replay the capture of) a created order.

    - Replay: an already-CAPTURED order returns the stored capture response.
    - Pre-send quote re-verification from the DB row: signature, expiry and
      money equality against the order row.
    - Transient exhaustion or a foreign ORDER_ALREADY_CAPTURED conflict ->
      PENDING_RECONCILIATION (then propagate; the API layer surfaces 503/409).
    """
    order = (
        await session.execute(
            select(OrderRow).where(OrderRow.paypal_order_id == paypal_order_id)
        )
    ).scalar_one_or_none()
    if order is None:
        raise OrderNotFoundError(f"unknown paypal order {paypal_order_id}")

    if order.state == "CAPTURED" and order.paypal_capture_id:
        # Idempotent replay of the stored success.
        return CaptureResponse(
            order_id=paypal_order_id,
            paypal_capture_id=order.paypal_capture_id,
            status="COMPLETED",
        )

    assert_transition(order.state, "CAPTURED")

    # ---- re-verify the quote from the DB row ------------------------------
    quote_row = await _load_quote_row(session, order.quote_id)
    _verify_query_matches(quote_row, order)
    try:
        verify_quote(_quote_from_row(quote_row), settings)
    except QuoteExpiredError:
        await _fail(session, order, "CAPTURE_FAILED")
        raise
    except QuoteError:
        await _fail(session, order, "CAPTURE_FAILED")
        raise

    # ---- PayPal capture ----------------------------------------------------
    request_id = request_id_for_order(paypal_order_id)
    try:
        response = await capture_paypal_order(
            transport, paypal_order_id=paypal_order_id, request_id=request_id
        )
    except PayPalTransientError:
        # Attempts exhausted: safe limbo state, deterministic request id lets
        # a later retry (or reconcile job) resume cleanly.
        await _fail(session, order, "PENDING_RECONCILIATION")
        raise
    except PayPalClientError as exc:
        if ALREADY_CAPTURED_ISSUE in exc.issues:
            # A capture succeeded through a different channel (e.g. buyer-side
            # JS SDK completed). We have no capture id stored -> reconcile.
            logger.warning(
                "capture of %s already captured elsewhere; marking PENDING_RECONCILIATION",
                paypal_order_id,
            )
            await _fail(session, order, "PENDING_RECONCILIATION")
            raise
        await _fail(session, order, "CAPTURE_FAILED")
        raise

    capture_id = extract_capture_id(response)
    prev_state = order.state
    assert_transition(prev_state, "CAPTURED")
    order.state = "CAPTURED"
    order.paypal_capture_id = capture_id

    # Atomic ledger + reputation in the same transaction.
    await post_capture_entries(session, order)

    await session.flush()

    # Stream: CAPTURED update. Published only after the request's commit
    # succeeds (get_session drain), so the feed can never run ahead of the
    # persisted capture. Capture replays short-circuit above -> no duplicates.
    enqueue_event(
        session, "transaction", "update",
        lambda s: transaction_row_for_order(s, order),
    )

    return CaptureResponse(
        order_id=paypal_order_id,
        paypal_capture_id=capture_id,
        status=str(response.get("status", "COMPLETED")),
    )


async def _fail(session: AsyncSession, order: OrderRow, to_state: str) -> None:
    """Record a safe transition WITHOUT raising (used inside except blocks).

    Commits immediately: the outer get_session helper would roll the whole
    transaction back when the exception keeps propagating, losing the
    PENDING_RECONCILIATION / CAPTURE_FAILED marker.
    """
    try:
        assert_transition(order.state, to_state)
        order.state = to_state
        # Stream: the failure state is persisted → publish its update after
        # this commit (feed always lags or matches the DB, never leads it).
        enqueue_event(
            session, "transaction", "update",
            lambda s: transaction_row_for_order(s, order),
        )
        await commit_and_emit(session)
    except IllegalOrderTransition:  # pragma: no cover - defensive
        logger.error(
            "refusing illegal failure transition %s -> %s for order %s",
            order.state, to_state, order.internal_order_id,
        )
