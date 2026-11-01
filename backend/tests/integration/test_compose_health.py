"""REQ-OPS-01: docker compose smoke — services are healthy end to end.

Skipped automatically when the stack is not running (CI runs docker compose
up in the release workflow; locally run `docker compose up -d` first).
"""

import os
import urllib.request

import pytest

BASE_URL = os.environ.get("SHIELD_E2E_BASE_URL", "http://localhost:8000")
FRONTEND_URL = os.environ.get("SHIELD_E2E_FRONTEND", "http://localhost:5173")


def _stack_up(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=2) as resp:  # nosec B310
            return resp.status == 200
    except OSError:
        return False


_requires_stack = pytest.mark.skipif(
    not _stack_up(f"{BASE_URL}/api/v1/health"),
    reason="docker compose stack not running (run `docker compose up -d` first)",
)


def _get(url: str, timeout: float = 5.0):
    with urllib.request.urlopen(url, timeout=timeout) as resp:  # nosec B310
        return resp.status, resp.read()


@_requires_stack
def test_compose_backend_health():
    status, body = _get(f"{BASE_URL}/api/v1/health")
    assert status == 200
    assert b"status" in body


@_requires_stack
def test_compose_storefront_serves_html():
    status, body = _get(FRONTEND_URL)
    assert status == 200
    assert b"root" in body


@_requires_stack
@pytest.mark.skipif(
    not os.environ.get("SHIELD_SANDBOX_CREDENTIALS"), reason="no Sandbox credentials in CI"
)
def test_smoke_render_health_sandbox_upstream():
    """Health must report the PayPal Sandbox connection state, never crash."""
    status, body = _get(f"{BASE_URL}/api/v1/health")
    assert status == 200
    assert b"paypal_sandbox_connected" in body
