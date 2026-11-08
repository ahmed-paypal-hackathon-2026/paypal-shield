"""PayPal webhook ingestion (Person 1, spec sections 2.2 / 2.6).

Pipeline:
1. Read the RAW body and the five PayPal transmission headers.
2. Verify via POST /v1/notifications/verify-webhook-signature using the
   configured webhook id. Only ``verification_status=SUCCESS`` proceeds...
   except when the operator has not configured a webhook id (dev/demo):
   then the event is accepted marked ``UNVERIFIED_DEMO`` (logged loudly),
   because Sandbox can still deliver simulated events worth processing.
3. Deduplicate: insert into ``webhook_events`` keyed by event id
   (PAYPAL-TRANSMISSION-ID, falling back to the payload's own id). A unique
   violation == duplicate delivery -> HTTP 200, zero side effects.
4. Process in a background task so the HTTP response returns fast:
   - PAYMENT.CAPTURE.COMPLETED -> reconcile capture (async confirmation)
   - PAYMENT.CAPTURE.REFUNDED  -> confirm PENDING refund to REFUNDED
   - CUSTOMER.DISPUTE.CREATED  -> set orders.dispute_active=TRUE
   - everything else -> stored + acknowledged, no side effects.

Processing is idempotent: every handler re-checks stored state first, and the
services it calls (ledger, refunds rows) are dedupe-guarded.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import OrderRow, WebhookEventRow
from app.services.claims import confirm_refund_webhook
from app.services.claims import reconcile_capture_webhook

logger = logging.getLogger(__name__)

HANDLED_EVENT_TYPES = frozenset(
    {
        "PAYMENT.CAPTURE.COMPLETED",
        "PAYMENT.CAPTURE.REFUNDED",
        "CUSTOMER.DISPUTE.CREATED",
    }
)

_UNVERIFIED = "UNVERIFIED_DEMO"
_VERIFIED = "VERIFIED"
_VERIFY_REQUEST_NS = uuid.uuid5(uuid.NAMESPACE_URL, "paypal-shield-webhook-verify-v1")


class WebhookVerificationError(Exception):
    """Signature verification failed -> HTTP 400, event NOT stored."""


# ---------------------------------------------------------------------------
# Verification (spec 2.6)
# ---------------------------------------------------------------------------
async def verify_webhook_signature(
    transport,
    settings,
    *,
    headers: dict[str, str],
    raw_body: bytes,
) -> str:
    """Return the verification mode applied: VERIFIED or UNVERIFIED_DEMO.

    Raises WebhookVerificationError on a FAILED verification (400 at API).
    """
    webhook_id = settings.paypal_webhook_id
    has_creds = settings.has_paypal_credentials
    if not (has_creds and webhook_id):
        logger.warning(
            "webhook received without webhook id configured; accepting as %s",
            _UNVERIFIED,
        )
        return _UNVERIFIED

    try:
        event: dict[str, Any] = json.loads(raw_body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise WebhookVerificationError(f"undecodable webhook body: {exc}") from None
    if not isinstance(event, dict):
        raise WebhookVerificationError("webhook body must be a JSON object")

    payload = {
        "auth_algo": headers.get("PAYPAL-AUTH-ALGO", ""),
        "cert_url": headers.get("PAYPAL-CERT-URL", ""),
        "transmission_id": headers.get("PAYPAL-TRANSMISSION-ID", ""),
        "transmission_sig": headers.get("PAYPAL-TRANSMISSION-SIG", ""),
        "transmission_time": headers.get("PAYPAL-TRANSMISSION-TIME", ""),
        "webhook_id": webhook_id,
        "webhook_event": event,
    }
    response = await transport.request(
        "POST",
        "/v1/notifications/verify-webhook-signature",
        request_id=str(
            uuid.uuid5(
                _VERIFY_REQUEST_NS,
                headers.get("PAYPAL-TRANSMISSION-ID") or str(event.get("id", "unknown")),
            )
        ),
        json_body=payload,
    )
    status = response.get("verification_status")
    if status == "SUCCESS":
        return _VERIFIED
    logger.warning("webhook signature verification=%s; rejecting", status)
    raise WebhookVerificationError(f"webhook verification_status={status}")


# ---------------------------------------------------------------------------
# Dedupe + persistence
# ---------------------------------------------------------------------------
def extract_event_id(headers: dict[str, str], event: dict[str, Any]) -> str:
    event_id = headers.get("PAYPAL-TRANSMISSION-ID", "")
    if not event_id:
        event_id = str(event.get("id", ""))
    return event_id or ""


async def store_event(
    session: AsyncSession, event_id: str, event_type: str, payload: dict[str, Any]
) -> bool:
    """Insert-first dedupe. Returns False when this delivery is a duplicate."""
    if not event_id:
        raise WebhookVerificationError("webhook has no event id")
    session.add(
        WebhookEventRow(
            event_id=event_id,
            event_type=event_type,
            payload=payload,
            processed_at=None,
        )
    )
    try:
        await session.flush()
        return True
    except IntegrityError:
        await session.rollback()
        logger.info("duplicate webhook delivery event_id=%s ignored", event_id)
        return False


# ---------------------------------------------------------------------------
# Background processing (idempotent by state re-check)
# ---------------------------------------------------------------------------
def _event_field(event: dict[str, Any], *path: str | int) -> Any:
    """Walk event.resource.<path> tolerating missing keys."""
    node: Any = event
    for key in path:
        if isinstance(node, dict) and isinstance(key, str):
            if key not in node:
                return None
            node = node[key]
        elif isinstance(node, list) and isinstance(key, int):
            if key < 0 or key >= len(node):
                return None
            node = node[key]
        else:
            return None
    return node


async def _find_order_by_capture_fallbacks(
    session: AsyncSession, resource: dict[str, Any]
) -> Optional[OrderRow]:
    """Match an order from a capture-shaped resource using several PayPal shapes."""
    capture_id = resource.get("id")
    if capture_id:
        row = (
            await session.execute(
                select(OrderRow).where(OrderRow.paypal_capture_id == str(capture_id))
            )
        ).scalar_one_or_none()
        if row:
            return row
    custom_id = resource.get("custom_id")  # we set internal order id at create
    if custom_id:
        try:
            key = uuid.UUID(str(custom_id))
        except ValueError:
            key = None
        if key is not None:
            row = await session.get(OrderRow, key)
            if row:
                return row
    related = (
        resource.get("supplementary_data", {}).get("related_ids", {}).get("order_id")
        if isinstance(resource.get("supplementary_data"), dict)
        else None
    )
    if related:
        row = (
            await session.execute(
                select(OrderRow).where(OrderRow.paypal_order_id == str(related))
            )
        ).scalar_one_or_none()
        return row
    return None


async def process_event(session: AsyncSession, payload: dict[str, Any]) -> str:
    """Apply one stored webhook event to domain state (idempotent)."""
    event_type = str(payload.get("event_type", ""))
    resource = payload.get("resource") or {}
    if not isinstance(resource, dict):
        logger.warning("webhook type=%s has a non-object resource", event_type)
        return "malformed"

    if event_type == "PAYMENT.CAPTURE.COMPLETED":
        order = await _find_order_by_capture_fallbacks(session, resource)
        if order is None:
            logger.info("capture-completed event matched no order; stored only")
            return "unmatched"
        return await reconcile_capture_webhook(session, order, resource)

    if event_type == "PAYMENT.CAPTURE.REFUNDED":
        return await confirm_refund_webhook(session, resource)

    if event_type == "CUSTOMER.DISPUTE.CREATED":
        order = await _find_order_by_capture_fallbacks(session, resource)
        if order is None:
            # disputes carry their own ids: try the dispute's order linkage
            tx = _event_field(
                payload, "resource", "disputed_transactions", 0, "seller_transaction_id"
            ) or _event_field(payload, "resource", "disputed_transactions", 0, "transaction_id")
            if tx:
                order = (
                    await session.execute(
                        select(OrderRow).where(OrderRow.paypal_capture_id == str(tx))
                    )
                ).scalar_one_or_none()
        if order is not None:
            order.dispute_active = True
        else:
            logger.warning("dispute event matched no order; stored only")
            return "unmatched"
        return "dispute_flagged"

    return "ignored"


async def mark_processed(session: AsyncSession, event_id: str) -> None:
    row = await session.get(WebhookEventRow, event_id)
    if row is not None:
        from datetime import datetime, timezone

        row.processed_at = datetime.now(timezone.utc)


__all__ = [
    "verify_webhook_signature",
    "extract_event_id",
    "store_event",
    "process_event",
    "mark_processed",
    "WebhookVerificationError",
    "HANDLED_EVENT_TYPES",
]
