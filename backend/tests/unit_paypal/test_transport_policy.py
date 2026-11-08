"""Transport retry/error policy (Person 1, spec section 2.8).

Every test drives httpx.MockTransport so no network is touched; the OAuth
token endpoint is part of the mock so PayPalAuth completes its flow.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from app.config import Settings
from app.services.paypal.auth import TokenManager
from app.services.paypal.errors import PayPalClientError, PayPalTransientError
from app.services.paypal.transport import PayPalTransport, _parse_issues


def _settings(**kw) -> Settings:
    base = dict(
        paypal_client_id="cid",
        paypal_client_secret="csecret",
        paypal_backoff_base_seconds=0.0005,  # keep tests instant
        paypal_request_timeout_seconds=2.0,
        _env_file=None,
    )
    base.update(kw)
    return Settings(**base)


def make_transport(handler, settings=None, alert_hook=None) -> tuple[PayPalTransport, httpx.AsyncClient]:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://api-m.sandbox.paypal.com",
    )
    tm = TokenManager(settings or _settings(), client=client)
    return PayPalTransport(settings or _settings(), tm, client=client, alert_hook=alert_hook), client


def _token_ok(request: httpx.Request) -> httpx.Response | None:
    if request.url.path.endswith("/oauth2/token"):
        return httpx.Response(
            200,
            json={"access_token": "tok", "token_type": "Bearer", "expires_in": 10800},
        )
    return None


class TestSuccessAndRecording:
    async def test_success_returns_body(self):
        seen = []

        def handler(request: httpx.Request) -> httpx.Response:
            tok = _token_ok(request)
            if tok:
                return tok
            seen.append(request)  # OAuth token calls are filtered out
            return httpx.Response(200, json={"id": "PAY-1", "status": "CREATED"})

        t, client = make_transport(handler)
        body = await t.request("POST", "/v2/checkout/orders", request_id="rid-1", json_body={})
        assert body == {"id": "PAY-1", "status": "CREATED"}
        assert seen[0].headers["PayPal-Request-Id"] == "rid-1"
        await client.aclose()


class TestRetryPolicy:
    async def test_429_retries_same_request_id_then_succeeds(self):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            tok = _token_ok(request)
            if tok:
                return tok
            if not request.url.path.endswith("/orders"):
                return httpx.Response(404, json={})
            calls.append(request.headers["PayPal-Request-Id"])
            if len(calls) < 3:
                return httpx.Response(429, json={"name": "RATE_LIMITED"})
            return httpx.Response(200, json={"ok": True})

        t, client = make_transport(handler)
        body = await t.request("POST", "/v2/checkout/orders", request_id="rid-x")
        assert body == {"ok": True}
        assert calls == ["rid-x", "rid-x", "rid-x"], "every attempt ships the same request id"
        await client.aclose()

    async def test_5xx_exhausted_raises_transient_error(self):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            tok = _token_ok(request)
            if tok:
                return tok
            calls.append(request.headers["PayPal-Request-Id"])
            return httpx.Response(500, json={"message": "internal"})

        t, client = make_transport(handler, settings=_settings(paypal_max_retries=3))
        with pytest.raises(PayPalTransientError) as excinfo:
            await t.request("POST", "/v2/checkout/orders", request_id="rid-y")
        assert excinfo.value.attempts == 3
        assert set(calls) == {"rid-y"}
        assert len(calls) == 3
        await client.aclose()

    async def test_timeout_is_retried_then_transient(self):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            tok = _token_ok(request)
            if tok:
                return tok
            calls.append(request)
            raise httpx.ConnectTimeout("simulated timeout", request=request)

        t, client = make_transport(handler, settings=_settings(paypal_max_retries=2))
        with pytest.raises(PayPalTransientError):
            await t.request("POST", "/v2/checkout/orders", request_id="rid-t")
        assert len(calls) == 2
        await client.aclose()

    async def test_backoff_is_exponential_with_jitter(self):
        delays: list[float] = []

        async def fake_sleep(d: float) -> None:
            delays.append(d)

        class FakeTransport:
            def __init__(self):
                self.calls = 0

            async def request(self, method, path, *, request_id, json_body=None):
                self.calls += 1
                return {}

        s = _settings(paypal_backoff_base_seconds=0.25, paypal_max_retries=5)
        t, _client = make_transport(lambda r: httpx.Response(200, json={}), settings=s)
        # Drive the private helper directly (unit of the backoff curve).
        orig = asyncio.sleep
        asyncio.sleep = fake_sleep  # type: ignore[assignment]
        try:
            await t._sleep_backoff(1)
            await t._sleep_backoff(2)
            await t._sleep_backoff(3)
        finally:
            asyncio.sleep = orig  # type: ignore[assignment]
        # expected midpoints: 0.25, 0.50, 1.00 with +-50% jitter
        assert 0.125 <= delays[0] <= 0.375
        assert 0.25 <= delays[1] <= 0.75
        assert 0.50 <= delays[2] <= 1.50
        await _client.aclose()


class TestClientErrors:
    async def test_400_never_retried_and_parses_issues(self):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            tok = _token_ok(request)
            if tok:
                return tok
            calls.append(request)
            return httpx.Response(
                400,
                json={
                    "name": "INVALID_REQUEST",
                    "debug_id": "abc123",
                    "details": [{"issue": "INVALID_CURRENCY_CODE"}],
                },
            )

        t, client = make_transport(handler)
        with pytest.raises(PayPalClientError) as excinfo:
            await t.request("POST", "/v2/checkout/orders", request_id="rid-z")
        assert excinfo.value.status_code == 400
        assert excinfo.value.issues == ["INVALID_CURRENCY_CODE"]
        assert excinfo.value.debug_id == "abc123"
        assert len(calls) == 1, "client errors are never retried"
        await client.aclose()

    async def test_item_total_mismatch_fires_alert_hook(self):
        alerts: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            tok = _token_ok(request)
            if tok:
                return tok
            return httpx.Response(
                400,
                json={
                    "debug_id": "d-1",
                    "details": [{"issue": "ITEM_TOTAL_MISMATCH"}],
                },
            )

        t, client = make_transport(handler, alert_hook=alerts.append)
        with pytest.raises(PayPalClientError):
            await t.request("POST", "/v2/checkout/orders", request_id="rid-m")
        assert alerts == ["ITEM_TOTAL_MISMATCH"]
        await client.aclose()

    async def test_item_total_mismatch_on_5xx_also_alerts(self):
        alerts: list[str] = []
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            tok = _token_ok(request)
            if tok:
                return tok
            calls.append(request)
            return httpx.Response(
                500,
                json={"debug_id": "d-2", "details": [{"issue": "ITEM_TOTAL_MISMATCH"}]},
            )

        t, client = make_transport(
            handler, settings=_settings(paypal_max_retries=2), alert_hook=alerts.append
        )
        with pytest.raises(PayPalTransientError):
            await t.request("POST", "/v2/checkout/orders", request_id="rid-m2")
        assert alerts == ["ITEM_TOTAL_MISMATCH"]
        await client.aclose()


class TestIssueParsing:
    def test_v2_details_shape(self):
        issues = _parse_issues(
            {"details": [{"issue": "A"}, {"issue": "B"}], "debug_id": "x"}
        )
        assert issues == ["A", "B"]

    def test_legacy_and_top_level_shapes(self):
        assert _parse_issues({"issues": [{"issue": "L"}]}) == ["L"]
        assert _parse_issues({"issue": "TOP"}) == ["TOP"]

    def test_no_body(self):
        assert _parse_issues({}) == []
        assert _parse_issues(None) == []
