"""OAuth token manager and auth flow (Person 1, REQ-BE-01).

- 100 concurrent calls trigger exactly one token request.
- Token refreshed when fewer than 120 s remain.
- On 401: cache invalidated, one refresh, one retry; second 401 raises.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from app.config import Settings
from app.services.paypal.auth import (
    CachedToken,
    PayPalAuth,
    PayPalAuthError,
    TokenManager,
)


def _settings(**kw) -> Settings:
    base = dict(
        paypal_client_id="cid",
        paypal_client_secret="csecret",
        oauth_refresh_margin_seconds=120,
        _env_file=None,
    )
    base.update(kw)
    return Settings(**base)


def _oauth_response(expires_in=10_800, token="tok-1") -> httpx.Response:
    return httpx.Response(
        200,
        json={"access_token": token, "token_type": "Bearer", "expires_in": expires_in},
        request=httpx.Request("POST", "https://api-m.sandbox.paypal.com/v1/oauth2/token"),
    )


class _Counter:
    def __init__(self, delay: float = 0.0) -> None:
        self.count = 0
        self.delay = delay

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.count += 1
        return _oauth_response()


class TestTokenCache:
    async def test_cached_token_not_refetched(self):
        c = _Counter()
        client = httpx.AsyncClient(transport=httpx.MockTransport(_static_handler(c)))
        tm = TokenManager(_settings(), client=client)
        t1 = await tm.get_token()
        t2 = await tm.get_token()
        assert t1 == t2 == "tok-1"
        assert c.count == 1
        await client.aclose()

    async def test_refresh_margin_triggers_refresh(self):
        c = _Counter()
        client = httpx.AsyncClient(transport=httpx.MockTransport(_static_handler(c)))
        # Margin 120 s but token lives 119 s -> usable immediately once, then refresh.
        tm = TokenManager(_settings(oauth_refresh_margin_seconds=120), client=client)
        first = await tm.get_token()
        # Manually age the token below the margin.
        older = CachedToken(access_token=first, expires_at=_now() + 100)
        tm._token = older
        second = await tm.get_token()
        assert c.count == 2
        assert tm.current().seconds_remaining() > 100
        assert tm.fetch_count == 2
        await client.aclose()

    async def test_single_flight_100_concurrent_one_fetch(self):
        c = _Counter(delay=0.02)
        client = httpx.AsyncClient(transport=httpx.MockTransport(_static_handler(c)))
        tm = TokenManager(_settings(), client=client)

        async def call():
            await asyncio.sleep(0)  # fan out scheduling
            return await tm.get_token()

        tokens = await asyncio.gather(*[call() for _ in range(100)])
        assert all(t == "tok-1" for t in tokens)
        assert c.count == 1, "single flight must collapse concurrent token refreshes"
        assert tm.fetch_count == 1
        await client.aclose()

    async def test_no_credentials_raises(self):
        client = httpx.AsyncClient(transport=httpx.MockTransport(_static_handler(_Counter())))
        tm = TokenManager(_settings(paypal_client_id="", paypal_client_secret=""), client=client)
        with pytest.raises(PayPalAuthError):
            await tm.get_token()
        assert not tm.has_credentials
        await client.aclose()

    async def test_bad_credentials_surface_paypal_auth_error(self):
        def deny(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"error": "invalid_client"},
                                  request=httpx.Request("POST", "/v1/oauth2/token"))

        client = httpx.AsyncClient(transport=httpx.MockTransport(deny))
        tm = TokenManager(_settings(), client=client)
        with pytest.raises(PayPalAuthError):
            await tm.get_token()
        await client.aclose()

    async def test_malformed_token_payload(self):
        def malformed(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"nope": True},
                                  request=httpx.Request("POST", "/v1/oauth2/token"))

        client = httpx.AsyncClient(transport=httpx.MockTransport(malformed))
        tm = TokenManager(_settings(), client=client)
        with pytest.raises(PayPalAuthError):
            await tm.get_token()
        await client.aclose()


def _now() -> float:
    import time

    return time.time()


def _static_handler(counter):
    def handler(request: httpx.Request) -> httpx.Response:
        if counter.delay:
            import time

            time.sleep(counter.delay)
        return counter.handler(request)

    return handler


class TestPayPalAuthFlow:
    async def test_401_invalidates_refreshes_and_retries_once(self):
        state = {"calls": 0, "token_requests": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/oauth2/token"):
                state["token_requests"] += 1
                return _oauth_response(token=f"tok-{state['token_requests']}")
            state["calls"] += 1
            if state["calls"] == 1:
                return httpx.Response(401, json={"message": "invalid token"},
                                      request=httpx.Request("GET", "https://api/pay"))
            return httpx.Response(200, json={"ok": True},
                                  request=httpx.Request("GET", "https://api/pay"))

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://api")
        tm = TokenManager(_settings(), client=client)
        resp = await client.get("/pay", auth=PayPalAuth(tm))
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}
        assert state["token_requests"] == 2  # initial + refresh
        assert state["calls"] == 2  # one retry
        await client.aclose()

    async def test_second_401_raises_paypal_auth_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/oauth2/token"):
                return _oauth_response()
            return httpx.Response(401, json={"message": "still invalid"},
                                  request=httpx.Request("GET", "https://api/pay"))

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://api")
        tm = TokenManager(_settings(), client=client)
        with pytest.raises(PayPalAuthError):
            await client.get("/pay", auth=PayPalAuth(tm))
        assert tm.current() is None  # cache invalidated on terminal 401
        await client.aclose()

    async def test_no_retry_on_other_status(self):
        state = {"calls": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/oauth2/token"):
                return _oauth_response()
            state["calls"] += 1
            return httpx.Response(400, json={"issue": "INVALID_REQUEST"},
                                  request=httpx.Request("GET", "https://api/pay"))

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://api")
        tm = TokenManager(_settings(), client=client)
        resp = await client.get("/pay", auth=PayPalAuth(tm))
        assert resp.status_code == 400
        assert state["calls"] == 1  # never auto-retry a client error
        await client.aclose()

    async def test_authorization_header_present(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/oauth2/token"):
                assert request.headers.get("Authorization", "").startswith("Basic")
                return _oauth_response()
            assert request.headers.get("Authorization", "").startswith("Bearer tok-1")
            return httpx.Response(200, json={},
                                  request=httpx.Request("GET", "https://api/pay"))

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://api")
        tm = TokenManager(_settings(), client=client)
        await client.get("/pay", auth=PayPalAuth(tm))
        await client.aclose()
