"""Actuarial Math Engine & HMAC Quote Signing (Person 2).

STRICT RULE: ZERO float types anywhere in this module.
All arithmetic uses decimal.Decimal with ROUND_HALF_UP exclusively.

Spec section 6.3 worked examples (verified):
    Trusted seller  → r_final = 7.80,  gate = COVERAGE_AVAILABLE, fee = 0.47
    AI scam         → r_final = 70.25, gate = COVERAGE_AVAILABLE, fee = 2.81
    Repeat offender → r_final = 81.00, gate = BLOCKED_HIGH_RISK,  fee = None

Weights (spec section 6.2):
    W_VIS   = 0.25
    W_SEM   = 0.20
    W_REV   = 0.15
    W_PRICE = 0.15
    W_MER   = 0.25

Fee formula:
    fee = max(MIN_FEE, min(price * 0.04, price * (r_final/100) * 0.05))

MIN_FEE by currency:
    USD, EUR, GBP → 0.25
    JPY            → 35
"""

from __future__ import annotations

import hashlib
import hmac as hmac_lib
import json
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from app.schemas.contracts import (
        PillarScores,
        QuoteRequest,
        RiskAssessment,
    )

# ---------------------------------------------------------------------------
# Weights (all Decimal — no float literals allowed)
# ---------------------------------------------------------------------------
W_VIS = Decimal("0.25")
W_SEM = Decimal("0.20")
W_REV = Decimal("0.15")
W_PRICE = Decimal("0.15")
W_MER = Decimal("0.25")

# Gate threshold
R_BLOCKED_THRESHOLD = Decimal("80.00")

# Fee constants
FEE_RATE_FLOOR_FACTOR = Decimal("0.04")   # 4% cap on item price
FEE_RATE_RISK_FACTOR = Decimal("0.05")    # 5% of risk-adjusted price

ZERO_DECIMAL = frozenset({"JPY", "HUF", "TWD"})

_MIN_FEES: dict[str, Decimal] = {
    "USD": Decimal("0.25"),
    "EUR": Decimal("0.25"),
    "GBP": Decimal("0.25"),
    "JPY": Decimal("35"),
    "HUF": Decimal("90"),
    "TWD": Decimal("8"),
}
_DEFAULT_MIN_FEE = Decimal("0.25")

# Quantization
_TWO_PLACES = Decimal("0.01")
_FOUR_PLACES = Decimal("0.0001")


def _q2(v: Decimal) -> Decimal:
    """Quantize to 2 decimal places, ROUND_HALF_UP."""
    return v.quantize(_TWO_PLACES, rounding=ROUND_HALF_UP)


def _q4(v: Decimal) -> Decimal:
    """Quantize to 4 decimal places, ROUND_HALF_UP."""
    return v.quantize(_FOUR_PLACES, rounding=ROUND_HALF_UP)


def _clamp01(v: Decimal) -> Decimal:
    return max(Decimal("0"), min(Decimal("1"), v))


# ---------------------------------------------------------------------------
# Core actuarial computations (underscore-prefixed: exported for unit tests)
# ---------------------------------------------------------------------------

def _compute_r_base(
    s_visual: Decimal,
    s_semantic: Decimal,
    s_reviews: Decimal,
    s_price: Decimal,
    s_merchant: Decimal,
) -> Decimal:
    """Weighted linear combination → R_base ∈ [0.0000, 1.0000].

    R_base = W_VIS*s_visual + W_SEM*s_semantic + W_REV*s_reviews
             + W_PRICE*s_price + W_MER*s_merchant
    """
    r = (
        W_VIS * s_visual
        + W_SEM * s_semantic
        + W_REV * s_reviews
        + W_PRICE * s_price
        + W_MER * s_merchant
    )
    return _clamp01(_q4(r))


def _compute_r_final(r_base: Decimal, m_penalty: Decimal) -> Decimal:
    """R_final = min(100.00, R_base * M_penalty * 100), rounded to 2dp."""
    r_final = r_base * m_penalty * Decimal("100")
    r_final = min(Decimal("100.00"), r_final)
    return _q2(r_final)


def _gate_decision(r_final: Decimal) -> "GateDecision":
    """Return the canonical GateDecision enum value."""
    from app.schemas.contracts import GateDecision  # lazy import
    if r_final > R_BLOCKED_THRESHOLD:
        return GateDecision.BLOCKED_HIGH_RISK
    return GateDecision.COVERAGE_AVAILABLE


def _compute_fee_amount(
    item_price: Decimal,
    r_final: Decimal,
    currency: str,
) -> Optional[Decimal]:
    """Compute protection fee amount (always returns a value, even for high-risk items).

    The gate decision (blocking) is handled by ``_gate_decision`` separately.
    This function purely applies the fee formula:

        fee = max(MIN_FEE, min(price * 0.04, price * (r_final/100) * 0.05))

    Args:
        item_price: The listing price of the protected item.
        r_final:    Final risk percentage [0.00, 100.00].
        currency:   ISO 4217 currency code.

    Returns:
        Computed Decimal fee.
        JPY: quantized to whole yen (ROUND_HALF_UP).
        Others: quantized to 2 decimal places.
        Returns None only if r_final > 100 (invalid input guard).
    """

    min_fee = _MIN_FEES.get(currency.upper(), _DEFAULT_MIN_FEE)
    price_cap = item_price * FEE_RATE_FLOOR_FACTOR
    risk_fee = item_price * (r_final / Decimal("100")) * FEE_RATE_RISK_FACTOR
    raw_fee = max(min_fee, min(price_cap, risk_fee))

    currency_upper = currency.upper()
    if currency_upper in ZERO_DECIMAL:
        return raw_fee.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return _q2(raw_fee)


# ---------------------------------------------------------------------------
# HMAC signing
# ---------------------------------------------------------------------------

def _build_sign_payload(
    quote_id: str,
    merchant_id: str,
    item_hash: str,
    item_price: Decimal,
    currency_code: str,
    protection_fee: Optional[Decimal],
    expires_at: str,
    is_eligible: bool = True,
) -> dict:
    return {
        "quote_id": quote_id,
        "merchant_id": merchant_id,
        "item_hash": item_hash,
        "item_price": str(item_price),
        "currency_code": currency_code,
        "protection_fee": str(protection_fee) if protection_fee is not None else "0.00",
        "expires_at": expires_at,
        "is_eligible": is_eligible,
    }


def _sign_payload(payload: dict, secret: str) -> str:
    """Return hex HMAC-SHA256 of the canonical JSON payload."""
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=True)
    sig = hmac_lib.new(
        secret.encode("utf-8"),
        canonical.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return sig


def sign_quote(
    quote_id: str,
    merchant_id: str,
    item_hash: str,
    item_price: Decimal,
    currency_code: str,
    hmac_secret: str,
    protection_fee: Optional[Decimal] = None,
    expires_at: str = "",
    is_eligible: bool = True,
    # Legacy kwargs accepted for backwards compatibility with __init__.py
    fee: Optional[Decimal] = None,
    **kwargs,
) -> str:
    """Build and sign a protection quote payload (HMAC-SHA256).

    The signed payload uses str() on Decimal fields to produce
    fixed-point strings compatible with Pydantic v2 JSON serialisation.
    Accepts both `protection_fee` and the legacy `fee` kwarg.
    The `is_eligible` flag is included in the signed payload so tampering
    with it changes the signature.
    """
    effective_fee = protection_fee if protection_fee is not None else fee
    payload = _build_sign_payload(
        quote_id=quote_id,
        merchant_id=merchant_id,
        item_hash=item_hash,
        item_price=item_price,
        currency_code=currency_code,
        protection_fee=effective_fee,
        expires_at=str(expires_at),
        is_eligible=is_eligible,
    )
    return _sign_payload(payload, hmac_secret)


# ---------------------------------------------------------------------------
# Public high-level wrappers (called by ShieldRiskEngine in __init__.py)
# ---------------------------------------------------------------------------

def compute_risk(
    pillars: "PillarScores",
    trust_score: Decimal,
    s_merchant: Decimal,
    m_penalty: Decimal,
    req: "QuoteRequest",
) -> "RiskAssessment":
    """Convert pillar scores + reputation into a RiskAssessment.

    This is the public wrapper used by ShieldRiskEngine.assess().
    Imports contracts at call time to avoid import-time pydantic dependency.
    """
    from app.schemas.contracts import GateDecision, RiskAssessment  # lazy import

    r_base = _compute_r_base(
        pillars.s_visual,
        pillars.s_semantic,
        pillars.s_reviews,
        pillars.s_price,
        s_merchant,
    )
    r_final = _compute_r_final(r_base, m_penalty)
    gate = _gate_decision(r_final)  # returns GateDecision enum

    return RiskAssessment(
        item_hash=req.item_hash,
        merchant_id=req.merchant_id,
        pillars=pillars,
        trust_score=trust_score,
        s_merchant=s_merchant,
        r_base=r_base,
        m_penalty=m_penalty,
        r_final=r_final,
        gate=gate,
    )


def compute_fee(
    assessment: "RiskAssessment",
    item_price: Decimal,
    currency: str,
) -> Decimal:
    """Compute the buyer protection fee for a given assessment.

    Returns Decimal("0.00") if the assessment gate is BLOCKED_HIGH_RISK.
    """
    from app.schemas.contracts import GateDecision  # lazy import
    if assessment.gate == GateDecision.BLOCKED_HIGH_RISK:
        return Decimal("0.00")
    return _compute_fee_amount(item_price, assessment.r_final, currency)
