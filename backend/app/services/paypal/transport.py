"""HTTP transport for the PayPal REST API (Person 1, spec section 2.8).

One method, one policy:

- **429 (rate limit)** -> exponential backoff with jitter (base 0.25 s),
  same ``PayPal-Request-Id`` on every attempt (PayPal dedupes by it).
- **5xx / network timeout** -> retry with the SAME request id, then raise
  ``PayPalTransientError`` so the caller marks PENDING_RECONCILIATION.
- **400 / 422 (other 4xx)** -> parse ``issues`` + ``debug_id`` and raise
  ``PayPalClientError``; NEVER retried.
- **401** -> handled transparently inside ``PayPalAuth`` (one refresh/retry).
- **``ITEM_TOTAL_MISMATCH``** among the issues -> injectable alert hook
  (default: CRITICAL log). This indicates a Shield-side bug, not a buyer
  problem, so it must page, not just log.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
from typing import Any, Optional

import httpx

from app.config import Settings
from app.services.paypal.auth import PayPalAuth, PayPalAuthError, TokenManager
from app.services.paypal.errors import (
    ALERT_ISSUE_ITEM_TOTAL,
    MismatchAlertHook,
    PayPalClientError,
    PayPalTransientError,
    _default_alert_hook,
)

logger = logging.getLogger(__name__)

# Statuses that are safe to retry with the same PayPal-Request-Id.
_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504, 520})


def _parse_issues(payload: Any) -> list[str]:
    """Extract issue names from a PayPal error body (v2 `details[].issue`,
    v1 legacy `issues[]`)."""
    issues: list[str] = []
    if not isinstance(payload, dict):
        return issues
    for detail in payload.get("details") or []:
        if isinstance(detail, dict) and detail.get("issue"):
            issues.append(str(detail["issue"]))
    for legacy in payload.get("issues") or []:
        if isinstance(legacy, dict) and legacy.get("issue"):
            issues.append(str(legacy["issue"]))
    if not issues and payload.get("issue"):
        issues.append(str(payload["issue"]))
    return issues


class PayPalTransport:
    """Thin async wrapper over httpx.AsyncClient implementing the retry policy."""

    def __init__(
        self,
        settings: Settings,
        token_manager: TokenManager,
        *,
        client: Optional[httpx.AsyncClient] = None,
        alert_hook: Optional[MismatchAlertHook] = None,
    ) -> None:
        self._settings = settings
        self._alert_hook: MismatchAlertHook = alert_hook or _default_alert_hook
        self._owned_client = client is None
        if client is None:
            client = httpx.AsyncClient(
                base_url=settings.paypal_api_base,
                timeout=settings.paypal_request_timeout_seconds,
            )
        self._client = client
        self._auth = PayPalAuth(token_manager)
        self._alerted: set[str] = set()

    def _maybe_alert(self, issues: list[str]) -> None:
        """High-severity alert, once per issue per transport (no re-paging)."""
        for issue in issues:
            if issue == ALERT_ISSUE_ITEM_TOTAL and issue not in self._alerted:
                self._alerted.add(issue)
                self._alert_hook(issue)

    # ------------------------------------------------------------------ core
    async def request(
        self,
        method: str,
        path: str,
        *,
        request_id: str,
        json_body: Optional[Any] = None,
    ) -> dict:
        """Send one PayPal API call with the full §2.8 retry/error policy.

        ``request_id`` goes out as ``PayPal-Request-Id`` on EVERY attempt —
        the caller derives it deterministically (quote/order/claim) so
        retries are idempotent at PayPal's edge.

        Returns the parsed JSON body (dict). Raises only
        ``PayPalClientError`` / ``PayPalTransientError`` / ``PayPalAuthError``.
        """
        headers = {
            "Content-Type": "application/json",
            "PayPal-Request-Id": request_id,
        }
        max_attempts = max(1, self._settings.paypal_max_retries)
        last_transient: Optional[str] = None
        self._alerted = set()  # dedupe within one logical call, not forever

        for attempt in range(1, max_attempts + 1):
            try:
                response = await self._client.request(
                    method, path, headers=headers, json=json_body, auth=self._auth
                )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_transient = f"{type(exc).__name__}: {exc}"
                if attempt < max_attempts:
                    await self._sleep_backoff(attempt)
                    continue
                raise PayPalTransientError(
                    f"PayPal unreachable after {max_attempts} attempts ({last_transient})",
                    request_id=request_id,
                    attempts=max_attempts,
                ) from None

            if response.status_code < 400:
                return self._decode(response)

            if response.status_code in _RETRYABLE_STATUS:
                body = self._safe_json(response)
                issues = _parse_issues(body)
                debug_id = str(body.get("debug_id", "")) if isinstance(body, dict) else ""
                self._maybe_alert(issues)
                last_transient = (
                    f"HTTP {response.status_code}"
                    + (f" debug_id={debug_id}" if debug_id else "")
                )
                if attempt < max_attempts:
                    await self._sleep_backoff(attempt)
                    continue
                raise PayPalTransientError(
                    f"PayPal transient failure {last_transient} "
                    f"after {max_attempts} attempts (same PayPal-Request-Id sent)",
                    request_id=request_id,
                    attempts=max_attempts,
                )

            # Everything else: client error -> parse + raise, never retry.
            body = self._safe_json(response)
            issues = _parse_issues(body)
            debug_id = str(body.get("debug_id", "")) if isinstance(body, dict) else ""
            self._maybe_alert(issues)
            names = ", ".join(issues) if issues else "unknown"
            raise PayPalClientError(
                f"PayPal rejected request: HTTP {response.status_code} issues=[{names}]"
                + (f" debug_id={debug_id}" if debug_id else ""),
                status_code=response.status_code,
                issues=issues,
                debug_id=debug_id,
                request_id=request_id,
            )

        raise PayPalTransientError(  # pragma: no cover - loop always returns/raises
            "unreachable retry loop state", request_id=request_id
        )

    async def _sleep_backoff(self, attempt: int) -> None:
        """Exponential backoff with jitter: base * 2^(attempt-1), +/-50%."""
        base = self._settings.paypal_backoff_base_seconds
        delay = base * (2 ** (attempt - 1))
        delay = delay * random.uniform(0.5, 1.5)
        logger.info("retrying PayPal call after %.3fs (attempt %s)", delay, attempt)
        await asyncio.sleep(delay)

    @staticmethod
    def _decode(response: httpx.Response) -> dict:
        try:
            body = response.json()
        except ValueError:
            return {}
        return body if isinstance(body, dict) else {"_body": body}

    @staticmethod
    def _safe_json(response: httpx.Response) -> dict:
        try:
            body = json.loads(response.text or "{}")
        except ValueError:
            return {}
        return body if isinstance(body, dict) else {}

    async def aclose(self) -> None:
        if self._owned_client:
            await self._client.aclose()


__all__ = [
    "PayPalTransport",
    "_parse_issues",
]
