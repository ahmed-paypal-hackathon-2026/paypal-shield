"""PayPal Captures Refund API client (Person 1, spec section 2.7).

POST /v2/payments/captures/{capture_id}/refund
Headers: PayPal-Request-Id = uuid5 derived from claim_id (deterministic ->
retries can never refund twice at PayPal's edge either).

Body (spec Listing 2):
    amount      = the claimed item price (NEVER the Buyer Protection Fee,
                  which is retained as the plan fee), never above the
                  captured item price
    custom_id   = claim id (correlates the later PAYMENT.CAPTURE.REFUNDED
                  webhook back to this claim)
    note_to_payer = fixed, buyer-safe text
"""

from __future__ import annotations

import logging
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from app.services.paypal.errors import PayPalClientError, PayPalTransientError
from app.services.paypal.orders import format_amount, request_id_for_refund

logger = logging.getLogger(__name__)

REFUND_NOTE = "PayPal Shield counterfeit claim approved"


def build_refund_body(
    *,
    claim_id: str,
    item_price: Decimal,
    claim_amount: Decimal,
    currency_code: str,
) -> dict[str, Any]:
    """Refund amount = min(claimed item price, captured item price).

    Clamped here (money must be right before send, matching the order
    composer's philosophy). Raises ValueError on nonsensical inputs.
    """
    zero_dec = currency_code in ("JPY", "HUF", "TWD")
    quantum = Decimal(1) if zero_dec else Decimal("0.01")
    if not item_price.is_finite() or not claim_amount.is_finite():
        raise ValueError("refund amounts must be finite decimals")
    try:
        item = item_price.quantize(quantum, rounding=ROUND_HALF_UP)
        claimed = claim_amount.quantize(quantum, rounding=ROUND_HALF_UP)
    except InvalidOperation:
        raise ValueError("refund amounts cannot be represented safely") from None
    if item <= 0 or claimed <= 0:
        raise ValueError("item and claim amounts must be positive")
    amount = min(claimed, item)
    return {
        "amount": {
            "currency_code": currency_code,
            "value": format_amount(amount, currency_code),
        },
        "custom_id": claim_id,
        "note_to_payer": REFUND_NOTE,
    }


async def refund_capture(
    transport,
    *,
    capture_id: str,
    claim_id: str,
    item_price: Decimal,
    claim_amount: Decimal,
    currency_code: str,
) -> dict[str, Any]:
    """Execute the refund call. Returns the parsed PayPal refund object.

    Raises PayPalTransientError (retry later / reconcile) or
    PayPalClientError (issue-code -> MANUAL_REVIEW at the flow layer).
    """
    body = build_refund_body(
        claim_id=claim_id,
        item_price=item_price,
        claim_amount=claim_amount,
        currency_code=currency_code,
    )
    response = await transport.request(
        "POST",
        f"/v2/payments/captures/{capture_id}/refund",
        request_id=request_id_for_refund(claim_id),
        json_body=body,
    )
    logger.info(
        "refund submitted claim=%s capture=%s status=%s",
        claim_id, capture_id, response.get("status", "?"),
    )
    return response


def extract_refund_status(refund_response: dict[str, Any]) -> str:
    status = (
        str(refund_response.get("status", "")).upper()
        if isinstance(refund_response, dict)
        else ""
    )
    if status not in ("COMPLETED", "PENDING"):
        raise PayPalClientError(
            "refund response missing a supported status",
            status_code=0,
            issues=["MALFORMED_REFUND_RESPONSE"],
        )
    return status


def extract_refund_id(refund_response: dict[str, Any]) -> str:
    rid = refund_response.get("id", "") if isinstance(refund_response, dict) else ""
    if rid:
        return str(rid)
    raise PayPalClientError(
        "refund response missing id",
        status_code=0,
        issues=["MALFORMED_REFUND_RESPONSE"],
    )


__all__ = [
    "build_refund_body",
    "refund_capture",
    "extract_refund_status",
    "extract_refund_id",
    "REFUND_NOTE",
    # re-exported for flow-layer imports convenience
    "PayPalClientError",
    "PayPalTransientError",
]
