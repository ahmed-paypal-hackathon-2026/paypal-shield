"""Deterministic no-network stand-in for PayPalTransport (Person 1).

Active ONLY when both hold (see app.main.create_app):
- no PayPal credentials configured, and
- ``DEMO_DETERMINISTIC_MODE=true``.

It serves canned PayPal-shaped responses for the same method signatures as
``PayPalTransport``, so order, capture, and approved-claim refund paths demo
end-to-end without touching the network. It never handles real money:
- ids are derived deterministically from the request id (replay-safe and
  restart-stable), exactly like the real request-id contract;
- the same (path, request_id) always returns the same response;
- every response is marked via the demo ``custom_id`` echoed from the body.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Optional

logger = logging.getLogger(__name__)

_NS = uuid.uuid5(uuid.NAMESPACE_URL, "paypal-shield-app-v1")


def _hex(ns_message: str) -> str:
    return uuid.uuid5(_NS, ns_message).hex


class DemoPayPalTransport:
    """Canned responses, identical call surface to PayPalTransport."""

    label = "demo"

    async def request(
        self,
        method: str,
        path: str,
        *,
        request_id: str,
        json_body: Optional[Any] = None,
    ) -> dict:
        if method == "POST" and path.rstrip("/") == "/v2/checkout/orders":
            return self._create_order(request_id, json_body)
        if method == "POST" and path.endswith("/refund") and "/captures/" in path:
            capture_id = path.split("/")[-2]
            return self._refund(capture_id, request_id, json_body)
        if method == "POST" and path.endswith("/capture"):
            paypal_order_id = path.split("/")[-2]
            return self._capture(paypal_order_id, request_id)
        raise AssertionError(f"demo transport: unexpected call {method} {path}")

    # ------------------------------------------------------------------ shapes
    def _create_order(self, request_id: str, body: Optional[Any]) -> dict:
        order_id = f"DEMO{_hex('demo-order:' + request_id)[:12].upper()}"
        total = "?"
        try:
            total = body["purchase_units"][0]["amount"]["value"]
        except (TypeError, KeyError, IndexError):  # pragma: no cover - defensive
            pass
        logger.info(
            "DEMO PayPal: create order id=%s total=%s (request_id=%s)",
            order_id, total, request_id,
        )
        return {
            "id": order_id,
            "status": "CREATED",
            "purchase_units": (
                body.get("purchase_units") if isinstance(body, dict) else []
            )
            or [],
            "links": [],
        }

    def _capture(self, paypal_order_id: str, request_id: str) -> dict:
        capture_id = f"DEMOCAP{_hex('demo-capture:' + paypal_order_id)[:10].upper()}"
        logger.info(
            "DEMO PayPal: capture order=%s capture=%s (request_id=%s)",
            paypal_order_id, capture_id, request_id,
        )
        return {
            "id": paypal_order_id,
            "status": "COMPLETED",
            "purchase_units": [
                {
                    "payments": {
                        "captures": [
                            {"id": capture_id, "status": "COMPLETED"}
                        ]
                    }
                }
            ],
        }

    def _refund(
        self,
        capture_id: str,
        request_id: str,
        body: Optional[Any],
    ) -> dict:
        refund_id = f"DEMOREF{_hex('demo-refund:' + request_id)[:10].upper()}"
        logger.info(
            "DEMO PayPal: refund capture=%s refund=%s (request_id=%s)",
            capture_id, refund_id, request_id,
        )
        payload = body if isinstance(body, dict) else {}
        return {
            "id": refund_id,
            "status": "COMPLETED",
            "amount": payload.get("amount", {}),
            "custom_id": payload.get("custom_id"),
        }


__all__ = ["DemoPayPalTransport"]
