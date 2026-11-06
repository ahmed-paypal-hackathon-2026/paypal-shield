"""Signed Buyer Protection Fee quotes (Person 1, REQ-BE-06).

HMAC-SHA256 over every quote field, constant-time verification, expiry, key
rotation and a one-way ``consumed`` flag. This module is the single HMAC
authority for the platform (Person 2 keeps ``actuarial.py`` purely as math;
its provisional ``sign_quote``/``build_quote`` are superseded by this module).

Signing scheme
--------------
canonical = "|".join(
    CONTRACT_VERSION, quote_id, merchant_id, item_hash,
    fmt(item_price), currency_code, fmt(protection_fee), iso(expires_at)
)
signature = HMAC_SHA256(key_canonical_or_sha256(key), canonical).hex()

``fmt`` renders a Decimal in fixed-point (never exponent, never float).
``iso`` renders UTC ISO-8601 with microseconds. Verification recomputes the
canonical string from the fields and compares with ``hmac.compare_digest``.
During key rotation a quote signed with the previous key stays valid.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Iterable, Optional

from app.config import Settings
from app.schemas.contracts import (
    GateDecision,
    ProtectionQuote,
    QuoteRequest,
    RiskAssessment,
)

logger = logging.getLogger(__name__)

_CONTRACT_VERSION = "v1"
_SEPARATOR = "|"


class QuoteError(Exception):
    """Base class for quote verification failures."""


class QuoteSignatureError(QuoteError):
    """Any signed field was changed (or the key does not match)."""


class QuoteExpiredError(QuoteError):
    """The quote is past its ``expires_at``."""


class QuoteConsumedError(QuoteError):
    """The quote has already been used for an order."""


def _fmt_decimal(value: Decimal) -> str:
    d = value if isinstance(value, Decimal) else Decimal(str(value))
    if not d.is_finite():
        raise QuoteError("non-finite decimal in quote payload")
    return format(d, "f")


def _fmt_datetime(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def canonical_quote_string(
    *,
    quote_id: str,
    merchant_id: str,
    item_hash: str,
    item_price: Decimal,
    currency_code: str,
    protection_fee: Decimal,
    expires_at: datetime,
) -> str:
    """Field-order-fixed canonical string; every element is HMAC-covered."""
    parts = [
        _CONTRACT_VERSION,
        str(quote_id),
        str(merchant_id),
        str(item_hash),
        _fmt_decimal(item_price),
        str(currency_code),
        _fmt_decimal(protection_fee),
        _fmt_datetime(expires_at),
    ]
    if any(_SEPARATOR in p for p in parts):
        raise QuoteError("reserved separator in quote payload")
    return _SEPARATOR.join(parts)


def sign_canonical(canonical: str, keys: Iterable[str]) -> str:
    """HMAC with the first key; keys are pre-hashed per RFC 2104 guidance."""
    keys = list(keys)
    if not keys:
        raise QuoteError("no HMAC key configured (QUOTES_HMAC_KEY)")
    first = keys[0]
    key_bytes = hashlib.sha256(first.encode("utf-8")).digest()
    return hmac.new(key_bytes, canonical.encode("utf-8"), hashlib.sha256).hexdigest()


def verify_signature_constant_time(
    key: str, canonical: str, expected_hex: str
) -> bool:
    """Constant-time equality of HMAC(key, canonical) and ``expected_hex``."""
    key_bytes = hashlib.sha256(key.encode("utf-8")).digest()
    recomputed = hmac.new(key_bytes, canonical.encode("utf-8"), hashlib.sha256).hexdigest()
    return hmac.compare_digest(recomputed, expected_hex)


def build_quote(
    assessment: RiskAssessment,
    req: QuoteRequest,
    settings: Settings,
    ttl_seconds: int | None = None,
    *,
    risk_engine=None,
    fallback=None,
) -> ProtectionQuote | None:
    """Create and sign a quote, or return None when coverage is unavailable."""
    if assessment.gate != GateDecision.COVERAGE_AVAILABLE:
        return None

    from app.services.engine_provider import get_risk_engine, fallback_engine

    engine = risk_engine if risk_engine is not None else get_risk_engine()
    safe_engine = fallback if fallback is not None else fallback_engine()
    try:
        fee = engine.price_fee(assessment, req.item_price, req.currency_code)
    except Exception:  # noqa: BLE001 - REQ-NF-03: never fail at checkout
        logger.exception("engine price_fee failed; using deterministic fallback")
        fee = safe_engine.price_fee(assessment, req.item_price, req.currency_code)

    if fee <= 0:
        return None

    quote_id = f"q_{uuid.uuid4().hex[:16]}"
    expires_at = datetime.now(timezone.utc) + timedelta(
        seconds=ttl_seconds or settings.quotes_ttl_seconds
    )
    canonical = canonical_quote_string(
        quote_id=quote_id,
        merchant_id=req.merchant_id,
        item_hash=req.item_hash,
        item_price=req.item_price,
        currency_code=req.currency_code,
        protection_fee=fee,
        expires_at=expires_at,
    )
    return ProtectionQuote(
        quote_id=quote_id,
        merchant_id=req.merchant_id,
        item_hash=req.item_hash,
        item_price=req.item_price,
        currency_code=req.currency_code,
        protection_fee=fee,
        expires_at=expires_at,
        hmac_signature=sign_canonical(canonical, settings.hmac_keys()),
    )


def verify_quote(
    quote: ProtectionQuote,
    settings: Settings,
    *,
    now: datetime | None = None,
    check_expiry: bool = True,
) -> None:
    """Verify signature (constant-time, rotation-aware) and expiry. Raises QuoteError."""
    now = now or datetime.now(timezone.utc)

    if check_expiry:
        expires_at = quote.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at <= now:
            raise QuoteExpiredError(f"quote {quote.quote_id} expired at {expires_at.isoformat()}")

    canonical = canonical_quote_string(
        quote_id=quote.quote_id,
        merchant_id=quote.merchant_id,
        item_hash=quote.item_hash,
        item_price=quote.item_price,
        currency_code=quote.currency_code,
        protection_fee=quote.protection_fee,
        expires_at=quote.expires_at,
    )

    keys = settings.hmac_keys()
    key_bytes_by_key = {
        k: hashlib.sha256(k.encode("utf-8")).digest() for k in keys
    }
    candidates = [
        hmac.new(kb, canonical.encode("utf-8"), hashlib.sha256).hexdigest()
        for kb in key_bytes_by_key.values()
    ]
    # Candidates are all computed up front; compares run against equal-length
    # digests with compare_digest only (no early-return timing channel).
    accepted = False
    for candidate in candidates:
        accepted = accepted or hmac.compare_digest(candidate, quote.hmac_signature)
    if not accepted:
        raise QuoteSignatureError(f"quote {quote.quote_id} failed HMAC verification")


# --------------------------------------------------------------------------
# Persistence helpers (quotes table) - consumed flag is one-way
# --------------------------------------------------------------------------
def persist_quote(session, quote: ProtectionQuote, r_final: Decimal, created_at: datetime | None = None) -> None:
    """Insert a QuoteRow. Idempotent-ish: same quote_id twice raises IntegrityError."""
    from app.db.models import QuoteRow

    session.add(
        QuoteRow(
            quote_id=quote.quote_id,
            merchant_id=quote.merchant_id,
            item_hash=quote.item_hash,
            item_price=quote.item_price,
            currency_code=quote.currency_code,
            protection_fee=quote.protection_fee,
            r_final=r_final,
            expires_at=quote.expires_at,
            hmac_signature=quote.hmac_signature,
            consumed=False,
            created_at=created_at,
        )
    )


async def load_active_quote(session, quote_id: str):
    """Fetch a quote row for order creation; raises QuoteError subclasses."""
    from sqlalchemy import select

    from app.db.models import QuoteRow

    row = (
        await session.execute(select(QuoteRow).where(QuoteRow.quote_id == quote_id))
    ).scalar_one_or_none()
    if row is None:
        raise QuoteSignatureError(f"unknown quote {quote_id}")
    if row.consumed:
        raise QuoteConsumedError(f"quote {quote_id} already consumed")
    return row


async def mark_consumed(session, quote_id: str) -> bool:
    """Flip ``consumed`` False->True exactly once. Returns False if already set."""
    from sqlalchemy import select, update

    from app.db.models import QuoteRow

    result = await session.execute(
        update(QuoteRow)
        .where(QuoteRow.quote_id == quote_id, QuoteRow.consumed.is_(False))
        .values(consumed=True)
    )
    return result.rowcount > 0
