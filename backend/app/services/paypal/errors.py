"""PayPal client error taxonomy + alert hook (Person 1, spec section 2.8).

- 4xx non-retryable (400/422): parsed ``issues`` + ``debug_id``; never retried.
- Transient (429, 5xx, timeouts): retried with the same PayPal-Request-Id;
  after attempts are exhausted the caller marks the object
  PENDING_RECONCILIATION.
- ``ITEM_TOTAL_MISMATCH`` triggers a high-severity alert: it indicates a
  shielding bug, not a buyer problem.
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

logger = logging.getLogger(__name__)

ALERT_ISSUE_ITEM_TOTAL = "ITEM_TOTAL_MISMATCH"

# Default high-severity alerting: CRITICAL log line (redacted by config).
MismatchAlertHook = Callable[[str], None]


def _default_alert_hook(issue: str) -> None:
    logger.critical(
        "HIGH SEVERITY ALERT: PayPal rejected an order with issue=%s "
        "- this indicates a Shield-side bug in the order payload/quote flow",
        issue,
    )


class PayPalError(Exception):
    """Base class for PayPal client errors."""


class PayPalClientError(PayPalError):
    """Non-retryable HTTP 400/422 (or other 4xx) from PayPal.

    Carries the parsed ``issues`` list and ``debug_id`` for alerting and
    safe surfacing. Callers must not auto-retry.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int,
        issues: Optional[list[str]] = None,
        debug_id: str = "",
        request_id: str = "",
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.issues = list(issues or [])
        self.debug_id = debug_id
        self.request_id = request_id


class PayPalTransientError(PayPalError):
    """429 / 5xx / timeout after all retry attempts were exhausted.

    The caller must mark the affected object PENDING_RECONCILIATION; the
    PayPal-Request-Id stayed identical across attempts so PayPal-side
    replays are safe.
    """

    def __init__(self, message: str, *, request_id: str = "", attempts: int = 0) -> None:
        super().__init__(message)
        self.request_id = request_id
        self.attempts = attempts


class OrderPayloadError(PayPalError):
    """Order payload failed our own strict equality invariant before sending.

    This is a Shield-side bug class: it must alert, never auto-retry and
    never reach PayPal.
    """


class OrderNotFoundError(PayPalError):
    """Capture/aggregation referenced an unknown internal or PayPal order id."""


class IllegalOrderTransition(PayPalError):
    """State machine violation for the order lifecycle (spec section 3.3)."""

    def __init__(self, from_state: str, to_state: str) -> None:
        super().__init__(f"illegal order transition: {from_state} -> {to_state}")
        self.from_state = from_state
        self.to_state = to_state
