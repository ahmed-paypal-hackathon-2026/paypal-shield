"""OAuth 2.0 token manager for the PayPal REST API (Person 1, REQ-BE-01).

- Token cached in process memory with absolute expiry from ``expires_in``.
- Refresh triggered when fewer than ``refresh_margin_seconds`` remain
  (default 120 s, spec section 2.3).
- Single-flight lock: 100 concurrent callers trigger exactly one token fetch.
- On HTTP 401 from any PayPal call the caller invalidates the cache and
  retries once (see ``PayPalAuth.clicks via httpx.Auth`` below); a second
  401 raises ``PayPalAuthError``.
- Client id/secret come only from settings (env vars); never logged, never
  returned by any API.
"""

from __future__ import annotations

import asyncio
import base64
import time
from dataclasses import dataclass
from typing import Any, Optional

import httpx

from app.config import Settings


class PayPalAuthError(Exception):
    """Raised when authentication cannot be established."""


@dataclass(frozen=True)
class CachedToken:
    access_token: str
    expires_at: float  # absolute unix epoch seconds
    token_type: str = "Bearer"

    def seconds_remaining(self, now: float | None = None) -> float:
        return self.expires_at - (now if now is not None else time.time())


class TokenManager:
    """In-memory token cache with single-flight refresh and 401 invalidation."""

    def __init__(
        self,
        settings: Settings,
        client: Optional[httpx.AsyncClient] = None,
        refresh_margin_seconds: int | None = None,
    ) -> None:
        self._settings = settings
        self._base_url = settings.paypal_api_base
        self._refresh_margin = refresh_margin_seconds or settings.oauth_refresh_margin_seconds
        self._client = client
        self._owned_client = client is None
        self._lock = asyncio.Lock()
        self._token: Optional[CachedToken] = None
        self._fetches = 0  # exposed for tests/diagnostics

    # ------------------------------------------------------------------ state
    @property
    def has_credentials(self) -> bool:
        return self._settings.has_paypal_credentials

    @property
    def fetch_count(self) -> int:
        return self._fetches

    def current(self) -> Optional[CachedToken]:
        return self._token

    def _is_usable(self, now: float) -> bool:
        tok = self._token
        return tok is not None and tok.seconds_remaining(now) >= self._refresh_margin

    async def invalidate(self) -> None:
        """Drop any cached token (called after a 401)."""
        self._token = None

    # ------------------------------------------------------------------ fetch
    def _client_for_flow(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._settings.paypal_request_timeout_seconds)
        return self._client

    async def _fetch_token(self) -> CachedToken:
        if not self.has_credentials:
            raise PayPalAuthError(
                "PAYPAL_CLIENT_ID / PAYPAL_CLIENT_SECRET are not configured"
            )
        basic = base64.b64encode(
            f"{self._settings.paypal_client_id}:{self._settings.paypal_client_secret}".encode()
        ).decode()
        client = self._client_for_flow()
        response = await client.post(
            f"{self._base_url}/v1/oauth2/token",
            headers={
                "Authorization": f"Basic {basic}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={"grant_type": "client_credentials"},
        )
        self._fetches += 1
        if response.status_code != 200:
            raise PayPalAuthError(
                f"token refresh failed: HTTP {response.status_code}"
            )
        payload: dict[str, Any] = response.json()
        access_token = payload.get("access_token")
        expires_in = payload.get("expires_in")
        if not access_token or not isinstance(expires_in, (int, float)):
            raise PayPalAuthError("malformed token response from PayPal")
        return CachedToken(
            access_token=str(access_token),
            expires_at=time.time() + float(expires_in),
            token_type=str(payload.get("token_type", "Bearer")),
        )

    async def get_token(self) -> str:
        """Return a valid access token, single-flight refreshing under load."""
        now = time.time()
        tok = self._token
        if tok is not None and tok.seconds_remaining(now) >= self._refresh_margin:
            return tok.access_token
        async with self._lock:  # single flight: one refresh at a time
            # Re-check inside the lock: another coroutine may have refreshed.
            tok = self._token
            now = time.time()
            if tok is not None and tok.seconds_remaining(now) >= self._refresh_margin:
                return tok.access_token
            fresh = await self._fetch_token()
            self._token = fresh
            return fresh.access_token

    async def refresh_forced(self) -> str:
        """Force one refresh (used on 401 retaliation by the auth flow)."""
        async with self._lock:
            fresh = await self._fetch_token()
            self._token = fresh
            return fresh.access_token

    async def aclose(self) -> None:
        if self._owned_client and self._client is not None:
            await self._client.aclose()
            self._client = None


class PayPalAuth(httpx.Auth):
    """httpx.Auth integration: Bearer header + 401 -> invalidate, refresh, retry once.

    Mechanical behavior for any PayPal call:
    1. attach ``Authorization: Bearer <token>``;
    2. on 401 response, invalidate the cache, fetch ONE new token, replay;
    3. on a second 401 or auth error, raise ``PayPalAuthError``.
    """

    requires_response_body = False

    def __init__(self, token_manager: TokenManager) -> None:
        self._manager = token_manager

    async def async_auth_flow(self, request: httpx.Request):
        token = await self._manager.get_token()
        request.headers["Authorization"] = f"Bearer {token}"
        response = yield request

        if response.status_code != 401:
            return

        # One refresh-and-retry, no more (spec section 2.3).
        await self._manager.invalidate()
        token = await self._manager.refresh_forced()
        request.headers["Authorization"] = f"Bearer {token}"
        response = yield request

        if response.status_code == 401:
            await self._manager.invalidate()
            raise PayPalAuthError("authentication failed after one token refresh")

    def auth_flow(self, request: httpx.Request):
        raise NotImplementedError("PayPal Shield uses the async client only")
