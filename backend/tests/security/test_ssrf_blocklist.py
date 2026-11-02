"""REQ-OPS-04 security tests: SSRF blocklist against the hardened fetcher.

Person 2 owns backend/app/ai/safe_fetch.py. These tests activate as soon as
that module lands and pin down the exact defensive behavior required by
spec section 6.8. Until then they document intent and skip cleanly.
"""

import pytest

sf = pytest.importorskip("app.ai.safe_fetch", reason="P2's safe_fetch module not landed yet")

BLOCKED_HOSTS = [
    "127.0.0.1",
    "localhost",
    "10.0.0.1",
    "172.16.5.9",
    "192.168.1.10",
    "169.254.169.254",  # cloud metadata
    "0.0.0.0",
    "::1",
    "fc00::1",
]
ALLOWED_HOSTS = ["images.example.com", "cdn.example.co.uk"]


def test_private_and_metadata_hosts_are_blocked():
    for host in BLOCKED_HOSTS:
        assert sf.is_blocked_host(host), f"{host} must be rejected"


def test_public_hosts_pass_the_blocklist():
    for host in ALLOWED_HOSTS:
        assert not sf.is_blocked_host(host), f"{host} must be allowed to proceed to fetch"


def test_url_policy_is_https_only_and_443_only():
    assert sf.is_blocked_url("http://images.example.com/a.jpg")
    assert sf.is_blocked_url("https://images.example.com:8080/a.jpg")
    assert sf.is_blocked_url("https://user:pass@images.example.com/a.jpg")
    assert not sf.is_blocked_url("https://images.example.com/a.jpg")


@pytest.mark.parametrize(
    "content_type", ["image/jpeg", "image/png", "image/webp"]
)
def test_allowed_image_content_types(content_type):
    assert sf.content_type_allowed(content_type)


def test_disallowed_content_types_rejected():
    assert not sf.content_type_allowed("text/html")
    assert not sf.content_type_allowed("image/svg+xml")
