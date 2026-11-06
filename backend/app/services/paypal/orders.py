"""PayPal Orders v2 composer + clients (Person 1).

Money rules (spec sections 1.4 / 6.4):
- ``format_amount`` emits zero-decimal strings for JPY/HUF/TWD and exactly
  2 decimals otherwise, always from ``decimal.Decimal`` (never float).
- ``build_order_payload`` enforces the strict equality BEFORE any HTTP call:
      amount.value == purchase_unit.amount.breakdown.item_total
                  == sum(unit_amount * quantity over every item)
  A mismatch raises ``OrderPayloadError`` — the payload must never reach
  PayPal wrong (which would page ``ITEM_TOTAL_MISMATCH``).
- The Buyer Protection Fee rides as its own ``DIGITAL_GOODS`` line item
  named exactly "Buyer Protection Fee (PayPal Shield)" (terminology rule).
- ``PayPal-Request-Id`` is derived deterministically so retries/duplicates
  collapse: uuid5 over a fixed app namespace.
"""

from __future__ import annotations

import uuid
from decimal import InvalidOperation, ROUND_HALF_UP, Decimal
from typing import Any, Optional

from app.config import ZERO_DECIMAL_CURRENCIES
from app.services.paypal.errors import OrderPayloadError

# Fixed application namespace for idempotency keys (spec section 3.3:
# "deterministic PayPal-Request-Id").
REQUEST_ID_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "paypal-shield-app-v1")

FEE_ITEM_NAME = "Buyer Protection Fee (PayPal Shield)"
FEE_ITEM_SKU = "SHIELD-FEE"

_QUANTA = {c: Decimal(1) for c in ZERO_DECIMAL_CURRENCIES}


def format_amount(value: Decimal, currency_code: str) -> str:
    """Canonical PayPal money string: 2 decimals, none for zero-decimal currencies."""
    if not isinstance(value, Decimal):
        try:
            value = Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ValueError(f"not a valid money value: {value!r}") from exc
    if currency_code in ZERO_DECIMAL_CURRENCIES:
        q = Decimal(1)
    else:
        q = Decimal("0.01")
    quantized = value.quantize(q, rounding=ROUND_HALF_UP)
    return format(quantized, "f")


def request_id_for_quote(quote_id: str) -> str:
    """Deterministic request id for order creation from a quote."""
    return str(uuid.uuid5(REQUEST_ID_NAMESPACE, f"create-order:{quote_id}"))


def request_id_for_order(paypal_order_id: str) -> str:
    """Deterministic request id for capture (or GET) from the PayPal order id."""
    return str(uuid.uuid5(REQUEST_ID_NAMESPACE, f"capture-order:{paypal_order_id}"))


def request_id_for_refund(claim_id: str) -> str:
    """Deterministic request id for the (single) refund of a claim."""
    return str(uuid.uuid5(REQUEST_ID_NAMESPACE, f"refund-claim:{claim_id}"))


class OrderItem:
    """One purchase-unit line item (item or the Buyer Protection Fee line)."""

    __slots__ = ("name", "unit_amount", "quantity", "category", "sku")

    def __init__(
        self,
        name: str,
        unit_amount: Decimal,
        quantity: int = 1,
        category: str = "PHYSICAL_GOODS",
        sku: Optional[str] = None,
    ) -> None:
        self.name = name
        self.unit_amount = unit_amount
        self.quantity = int(quantity)
        self.category = category
        self.sku = sku

    def line_total(self, currency_code: str) -> Decimal:
        quantum = Decimal(1) if currency_code in _QUANTA else Decimal("0.01")
        return (self.unit_amount * self.quantity).quantize(
            quantum, rounding=ROUND_HALF_UP
        )

    def to_paypal(self, currency_code: str) -> dict[str, Any]:
        item: dict[str, Any] = {
            "name": self.name,
            "quantity": str(self.quantity),
            "unit_amount": {
                "currency_code": currency_code,
                "value": format_amount(self.unit_amount, currency_code),
            },
            "category": self.category,
        }
        if self.sku:
            item["sku"] = self.sku
        return item


def fee_line_item(protection_fee: Decimal) -> OrderItem:
    """The explicit second line item carrying the Buyer Protection Fee.

    Category DIGITAL_GOODS: the fee is a platform service, not a shipped
    good, so it never confuses shipping/physical-goods validation.
    """
    return OrderItem(
        name=FEE_ITEM_NAME,
        unit_amount=protection_fee,
        quantity=1,
        category="DIGITAL_GOODS",
        sku=FEE_ITEM_SKU,
    )


def build_order_payload(
    *,
    paypal_reference_id: str,  # our internal order id (custom_id / reference)
    item_price: Decimal,
    item_name: str,
    currency_code: str,
    protection_fee: Optional[Decimal],
) -> dict[str, Any]:
    """Compose a /v2/checkout/orders body with the strict Decimal equality.

    The buy-side invariant is checked BEFORE sending:
        amount.value == item_total == Σ(unit_amount × quantity)
    A violation raises OrderPayloadError (Shield bug -> alert at the
    transport/flow layer, never a PayPal round-trip).
    """
    items: list[OrderItem] = [
        OrderItem(name=item_name, unit_amount=item_price, quantity=1)
    ]
    fee = Decimal("0")
    if protection_fee is not None and protection_fee > 0:
        items.append(fee_line_item(protection_fee))
        fee = protection_fee

    total = (item_price + fee).quantize(
        Decimal("0.01") if currency_code not in _QUANTA else Decimal(1),
        rounding=ROUND_HALF_UP,
    )

    # ---- strict equality gate (pre-send, pure Decimal) -------------------
    item_total = sum(
        (item.line_total(currency_code) for item in items), Decimal("0")
    )
    problems: list[str] = []
    if item_total != total:
        problems.append(
            f"item_total {item_total} != amount.value {total}"
        )
    recomputed = sum(
        (item.line_total(currency_code) for item in items
         if item.category != "DIGITAL_GOODS"),
        Decimal("0"),
    )
    fee_lines_total = sum(
        (item.line_total(currency_code) for item in items
         if item.category == "DIGITAL_GOODS"),
        Decimal("0"),
    )
    if fee_lines_total != fee:
        problems.append(
            f"fee line total {fee_lines_total} != protection_fee {fee}"
        )
    if protection_fee and protection_fee.quantize(
        Decimal("0.01") if currency_code not in _QUANTA else Decimal(1),
        rounding=ROUND_HALF_UP,
    ) >= item_price.quantize(
        Decimal("0.01") if currency_code not in _QUANTA else Decimal(1),
        rounding=ROUND_HALF_UP,
    ):
        problems.append(
            f"protection_fee {protection_fee} >= item_price {item_price}; "
            "quote should have been declined"
        )
    if problems:
        raise OrderPayloadError(
            "order payload failed the strict money invariant: " + "; ".join(problems)
        )

    value_str = format_amount(total, currency_code)
    item_total_str = format_amount(item_total, currency_code)
    if value_str != item_total_str:
        # String-level check too: PayPal compares the rendered values.
        raise OrderPayloadError(
            f"rendered money mismatch: value={value_str} item_total={item_total_str}"
        )

    return {
        "intent": "CAPTURE",
        "purchase_units": [
            {
                "custom_id": paypal_reference_id,
                "description": item_name,
                "amount": {
                    "currency_code": currency_code,
                    "value": value_str,
                    "breakdown": {
                        "item_total": {
                            "currency_code": currency_code,
                            "value": item_total_str,
                        }
                    },
                },
                "items": [item.to_paypal(currency_code) for item in items],
            }
        ],
    }


async def create_paypal_order(
    transport,
    *,
    request_id: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """POST /v2/checkout/orders. Returns the PayPal order object."""
    return await transport.request(
        "POST", "/v2/checkout/orders", request_id=request_id, json_body=payload
    )


async def capture_paypal_order(
    transport,
    *,
    paypal_order_id: str,
    request_id: str,
) -> dict[str, Any]:
    """POST /v2/checkout/orders/{id}/capture. Returns the capture object."""
    return await transport.request(
        "POST",
        f"/v2/checkout/orders/{paypal_order_id}/capture",
        request_id=request_id,
    )


def extract_capture_id(capture_response: dict[str, Any]) -> str:
    """Pull the capture id out of a capture response (defensive paths)."""
    try:
        unit = capture_response["purchase_units"][0]
        cap = unit["payments"]["captures"][0]
        return str(cap["id"])
    except (KeyError, IndexError, TypeError):
        pass
    # Fallback: some response shapes put capture under status links.
    for link in capture_response.get("links", []) or []:
        if link.get("rel") == "self":  # pragma: no cover - defensive
            continue
    raise OrderPayloadError("capture response missing capture id")


def extract_capture_status(capture_response: dict[str, Any]) -> str:
    try:
        return str(capture_response["status"])
    except (KeyError, TypeError):  # pragma: no cover - defensive
        return ""


__all__ = [
    "format_amount",
    "request_id_for_quote",
    "request_id_for_order",
    "request_id_for_refund",
    "OrderItem",
    "fee_line_item",
    "build_order_payload",
    "create_paypal_order",
    "capture_paypal_order",
    "extract_capture_id",
    "extract_capture_status",
]
