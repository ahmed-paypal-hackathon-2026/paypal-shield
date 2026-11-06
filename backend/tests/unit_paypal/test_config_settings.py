"""Sandbox guard, env-only config and log redaction (Person 1)."""

from __future__ import annotations

import logging

import pytest

from app.config import (
    PAYPAL_SANDBOX_BASE_URL,
    SandboxGuardError,
    SecretRedactingFilter,
    Settings,
)


def _settings(**kwargs) -> Settings:
    return Settings(_env_file=None, **kwargs)


class TestSandboxGuard:
    def test_default_is_sandbox(self):
        s = _settings()
        assert s.is_sandbox
        s.enforce_sandbox()  # no raise

    def test_non_sandbox_without_override_raises(self, monkeypatch):
        monkeypatch.delenv("ALLOW_NON_SANDBOX", raising=False)
        s = _settings(paypal_api_base="https://api-m.paypal.com", allow_non_sandbox=False)
        with pytest.raises(SandboxGuardError):
            s.enforce_sandbox()

    def test_non_sandbox_with_explicit_override_passes(self):
        s = _settings(paypal_api_base="https://api-m.paypal.com", allow_non_sandbox=True)
        s.enforce_sandbox()  # no raise

    def test_trailing_slash_normalized(self):
        s = _settings(paypal_api_base=PAYPAL_SANDBOX_BASE_URL + "/")
        assert s.is_sandbox

    def test_effective_database_url_fallback_chain(self, tmp_path):
        s = _settings()
        assert s.effective_database_url == s._anchor_sqlite("sqlite+aiosqlite:///./shield.db")
        assert s.effective_database_url.startswith("sqlite")
        s2 = _settings(sqlite_url="sqlite+aiosqlite:///./x.db")
        # relative sqlite paths are anchored at the repo root (CWD-independent)
        assert s2.effective_database_url.endswith(("x.db", "x.db"))
        assert str(s2._repo_root) in s2.effective_database_url
        s3 = _settings(
            sqlite_url="sqlite+aiosqlite:///./x.db",
            database_url="postgresql+psycopg://u:p@h/db",
        )
        assert s3.effective_database_url.startswith("postgresql")

    def test_sqlite_absolute_path_not_anchored(self):
        s = _settings(sqlite_url=r"sqlite+aiosqlite:///C:/tmp/x.db")
        assert s.effective_database_url == r"sqlite+aiosqlite:///C:/tmp/x.db"

    def test_zero_decimal_currencies(self):
        from app.config import ZERO_DECIMAL_CURRENCIES

        assert ZERO_DECIMAL_CURRENCIES == frozenset({"JPY", "HUF", "TWD"})


class TestSecretRedaction:
    def _record(self, message: str) -> logging.LogRecord:
        return logging.LogRecord(
            name="t", level=logging.INFO, pathname=__file__, lineno=1,
            msg=message, args=(), exc_info=None,
        )

    def test_literal_secret_redacted(self):
        f = SecretRedactingFilter(["super-secret-value"])
        rec = self._record("calling paypal with super-secret-value in body")
        assert f.filter(rec) is True
        assert "super-secret-value" not in rec.getMessage()
        assert "***REDACTED***" in rec.getMessage()

    def test_access_token_key_redacted(self):
        f = SecretRedactingFilter([])
        rec = self._record('{"access_token": "A21F.-verylongtoken", "expires_in": 10800}')
        f.filter(rec)
        assert "A21F.-verylongtoken" not in rec.getMessage()

    def test_bearer_header_redacted(self):
        f = SecretRedactingFilter([])
        rec = self._record("Authorization: Bearer abc.def-ghi sent upstream")
        f.filter(rec)
        assert "abc.def-ghi" not in rec.getMessage()

    def test_hmac_signature_field_redacted(self):
        f = SecretRedactingFilter([])
        rec = self._record('sig hmac_signature=cafef00dbadf00d stored')
        f.filter(rec)
        assert "cafef00dbadf00d" not in rec.getMessage()

    def test_settings_secret_values_collected(self):
        s = _settings(
            paypal_client_secret="sec1",
            quotes_hmac_key="key1",
            claim_reviewer_token="rev1",
        )
        assert set(s.secret_values()) == {"sec1", "key1", "rev1"}


class TestHmacKeyRotation:
    def test_keys_current_first(self):
        s = _settings(quotes_hmac_key="new", quotes_hmac_key_previous="old")
        assert s.hmac_keys() == ["new", "old"]

    def test_no_keys(self):
        assert _settings().hmac_keys() == []
