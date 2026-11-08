"""PayPal Shield application configuration (Person 1).

Every setting comes from environment variables only (spec REQ-BE area,
section 7.3). No secret is ever hard-coded and no secret is ever returned
by an API response. ``SecretRedactingFilter`` keeps secrets out of logs.

Sandbox guard: the application refuses to start against any non-Sandbox
PayPal base URL unless ``ALLOW_NON_SANDBOX=true`` is set explicitly
(spec section 7.3, "Sandbox guard").
"""

from __future__ import annotations

import logging
import re
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Optional

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PAYPAL_SANDBOX_BASE_URL = "https://api-m.sandbox.paypal.com"

# Currencies with no decimals per PayPal rules (spec section 6.4).
ZERO_DECIMAL_CURRENCIES = frozenset({"JPY", "HUF", "TWD"})

_SUPPORTED_CURRENCIES = frozenset({"USD", "EUR", "GBP", "JPY", "HUF", "TWD"})


class ConfigurationError(RuntimeError):
    """Raised when configuration is unsafe or incomplete."""


class SandboxGuardError(ConfigurationError):
    """Raised when PayPal base URL is not the Sandbox URL and override is off."""


class Settings(BaseSettings):
    """Env-only settings filled by pydantic-settings."""

    # ``.env`` resolution is CWD-independent: prefer the working directory's
    # .env when it exists (per-run override), else the repository root's .env
    # so ``uvicorn``/alembic/pytest all find it regardless of where they run.
    _repo_root = Path(__file__).resolve().parents[2]
    model_config = SettingsConfigDict(
        env_file=Path(".env") if Path(".env").is_file() else _repo_root / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Application ------------------------------------------------------
    log_level: str = "INFO"
    api_v1_prefix: str = "/api/v1"

    # --- PayPal Sandbox ---------------------------------------------------
    paypal_api_base: str = PAYPAL_SANDBOX_BASE_URL
    allow_non_sandbox: bool = False
    paypal_client_id: str = ""
    paypal_client_secret: str = ""
    paypal_webhook_id: str = ""

    # --- OAuth token manager ----------------------------------------------
    oauth_refresh_margin_seconds: int = 120
    paypal_request_timeout_seconds: float = 10.0

    # --- Quote signing ------------------------------------------------------
    quotes_hmac_key: str = ""
    quotes_hmac_key_previous: str = ""  # accepted during rotation window
    quotes_ttl_seconds: int = 600
    hmac_rotate_tolerate_missing_previous: bool = True

    # --- Databases ----------------------------------------------------------
    database_url: str = ""
    sqlite_url: str = ""

    # --- Feature flags ------------------------------------------------------
    demo_deterministic_mode: bool = True  # bridges to P2's SHIELD_DEMO_MODE
    engine_impl: str = "app_ai"  # "app_ai" (Person 2) | "stub" (deterministic fallback)

    # --- Admin / reviewer -----------------------------------------------------
    claim_reviewer_token: str = ""

    # --- HTTP (later phases: CORS, webhooks, retries) -------------------------
    allowed_origins: str = "http://localhost:5173,http://localhost:4173"
    paypal_max_retries: int = 3
    paypal_backoff_base_seconds: float = 0.25

    # --- Claims / arbiter (Phase 3, spec sections 3.4 / 6.6) ------------------
    # All knobs are configuration so Person 2/4 can recalibrate without code.
    claim_window_days: int = Field(default=30, gt=0)
    claim_auto_payout_cap: Decimal = Field(default=Decimal("250.00"), gt=0)
    claim_velocity_window_days: int = Field(default=30, gt=0)
    claim_velocity_max_claims: int = Field(default=3, gt=0)
    claim_velocity_max_ratio: Decimal = Field(
        default=Decimal("0.10"), gt=0, le=1
    )
    claim_photo_max_bytes: int = Field(default=5 * 1024 * 1024, gt=0)

    # --- Stream API (Phase 4, contract /api/v1/stream) ------------------------
    # SSE keep-alive comment ("​: ping") cadence in seconds; keeps proxies from
    # idling an open text/event-stream connection out.
    stream_heartbeat_seconds: int = Field(default=15, gt=0, le=600)

    @field_validator("paypal_api_base")
    @classmethod
    def _strip_base(cls, v: str) -> str:
        return v.strip().rstrip("/")

    # ------------------------------------------------------------------ helpers
    def _anchor_sqlite(self, url: str) -> str:
        """Anchor relative sqlite file paths at the repo root (CWD-independent)."""
        if not url.startswith("sqlite") or "///" not in url:
            return url
        scheme, _, rest = url.partition("///")
        p = Path(rest)
        if p.is_absolute():
            return url
        return f"{scheme}///{self._repo_root / p}"

    @property
    def effective_database_url(self) -> str:
        """Postgres in deployment, SQLite locally (spec section 1.6)."""
        if self.database_url:
            return self.database_url
        if self.sqlite_url:
            return self._anchor_sqlite(self.sqlite_url)
        return self._anchor_sqlite("sqlite+aiosqlite:///./shield.db")

    @property
    def is_sqlite(self) -> bool:
        return self.effective_database_url.startswith("sqlite")

    @property
    def is_sandbox(self) -> bool:
        """True when the PayPal base URL is exactly the Sandbox URL."""
        return self.paypal_api_base.startswith(PAYPAL_SANDBOX_BASE_URL)

    @property
    def allowed_origin_list(self) -> list[str]:
        return [o.strip() for o in self.allowed_origins.split(",") if o.strip()]

    @property
    def has_paypal_credentials(self) -> bool:
        return bool(self.paypal_client_id and self.paypal_client_secret)

    def enforce_sandbox(self) -> None:
        """Raise unless the base URL is the Sandbox URL or explicitly overridden."""
        if not self.is_sandbox and not self.allow_non_sandbox:
            raise SandboxGuardError(
                "Refusing to start against a non-Sandbox PayPal base URL "
                f"({self.paypal_api_base}). Set PAYPAL_API_BASE="
                f"{PAYPAL_SANDBOX_BASE_URL} or ALLOW_NON_SANDBOX=true "
                "to acknowledge the override."
            )

    def hmac_keys(self) -> list[str]:
        """Signing/verification keys, current first (rotation support)."""
        keys: list[str] = []
        if self.quotes_hmac_key:
            keys.append(self.quotes_hmac_key)
        if self.quotes_hmac_key_previous:
            keys.append(self.quotes_hmac_key_previous)
        return keys

    def secret_values(self) -> list[str]:
        """Literal secret strings that must never appear in logs."""
        return [
            s
            for s in (
                self.paypal_client_secret,
                self.quotes_hmac_key,
                self.quotes_hmac_key_previous,
                self.claim_reviewer_token,
            )
            if s
        ]


def get_settings() -> Settings:
    return Settings()  # fresh instance; cached accessor is settings_container()


@lru_cache(maxsize=1)
def settings_container() -> Settings:
    """Process-wide settings singleton (created once at startup)."""
    return Settings()


# --------------------------------------------------------------------------
# Log redaction (spec section 7.3: "secrets ... never logged")
# --------------------------------------------------------------------------
class SecretRedactingFilter(logging.Filter):
    """Replace known secret literals and token-shaped strings in log records.

    Works on the fully formatted message so the redaction also covers
    exception text and structured payloads.
    """

    _TOKEN_RE = re.compile(
        r'(?:access_token|hmac_signature|authorization)[\s"\']*[:=][\s"\']*"?[A-Za-z0-9._\-]+',
        re.IGNORECASE,
    )
    _BEARER_RE = re.compile(r"(Bearer\s+)[A-Za-z0-9._\-]+")

    def __init__(self, secrets: Optional[list[str]] = None) -> None:
        super().__init__()
        self._secrets = [s for s in (secrets or []) if s]
        self._replacement = "***REDACTED***"

    def _redact(self, message: str) -> str:
        for secret in self._secrets:
            if secret:
                message = message.replace(secret, self._replacement)
        # Bearer tokens first (otherwise the authorization key rule truncates
        # at the space and leaves the token behind).
        message = self._BEARER_RE.sub(r"\1" + self._replacement, message)
        message = self._TOKEN_RE.sub(self._replacement, message)
        return message

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        try:
            message = record.getMessage()
        except Exception:  # formatting itself failed; leave record untouched
            return True
        redacted = self._redact(message)
        if redacted != message:
            record.msg = redacted
            record.args = ()
        return True


def configure_logging(settings: Settings) -> None:
    """Root logging with secret redaction on every handler."""
    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    redactor = SecretRedactingFilter(settings.secret_values())
    root = logging.getLogger()
    for handler in root.handlers:
        handler.addFilter(redactor)


def apply_demo_mode_bridge(settings: Settings) -> None:
    """Bridge P4's ``DEMO_DETERMINISTIC_MODE`` to P2's ``SHIELD_DEMO_MODE``.

    Person 2's ``app.ai.demo_cache`` reads ``SHIELD_DEMO_MODE`` directly from
    the environment. We must not edit ``app/ai/`` (ownership boundary), so the
    app sets the variable during startup when P4's flag is enabled.
    """
    if settings.demo_deterministic_mode:
        import os

        os.environ.setdefault("SHIELD_DEMO_MODE", "1")
    else:
        import os

        os.environ["SHIELD_DEMO_MODE"] = "0"
