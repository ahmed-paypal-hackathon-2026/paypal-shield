"""Pillar 5 — Bayesian Merchant Reputation Scoring (Person 2).

Bayesian prior: m = 10, C = 0.70
TrustScore = (successful_orders + m * C) / (total_orders + m)
           = (successful_orders + 7) / (total_orders + 10)

S_merchant = 1.0000 - TrustScore  (clamped to [0.00, 1.00])

Fraud penalty multiplier M_penalty:
    0 verified_fraud_claims → 1.00
    1 verified_fraud_claims → 1.50
    ≥ 2 verified_fraud_claims → 2.00
"""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from typing import List, Tuple, TYPE_CHECKING

if TYPE_CHECKING:
    pass  # MerchantReputation imported at runtime to avoid circular refs

_FOUR_PLACES = Decimal("0.0001")
_TWO_PLACES = Decimal("0.01")
_CLAMP_MIN = Decimal("0.0000")
_CLAMP_MAX = Decimal("1.0000")

# Bayesian prior parameters (spec section 6.5)
_PRIOR_M = Decimal("10")
_PRIOR_C = Decimal("0.70")
_PRIOR_MC = _PRIOR_M * _PRIOR_C  # = 7

# Fraud penalty multipliers
_PENALTY_ZERO = Decimal("1.00")
_PENALTY_ONE = Decimal("1.50")
_PENALTY_TWO_PLUS = Decimal("2.00")

_REASON_FLAGGED = "REP_FLAGGED_HISTORY"
_REASON_VERIFIED = "REP_VERIFIED_HISTORY"


def _clamp(value: Decimal) -> Decimal:
    return max(_CLAMP_MIN, min(_CLAMP_MAX, value))


def _quantize4(value: Decimal) -> Decimal:
    return value.quantize(_FOUR_PLACES, rounding=ROUND_HALF_UP)


def _quantize2(value: Decimal) -> Decimal:
    return value.quantize(_TWO_PLACES, rounding=ROUND_HALF_UP)


def score_reputation(rep: "MerchantReputation") -> Tuple[Decimal, Decimal, Decimal, List[str]]:  # noqa: F821
    """Compute Bayesian trust score, merchant risk score, and fraud penalty.

    Args:
        rep: MerchantReputation instance from contracts.py.

    Returns:
        (trust_score, s_merchant, m_penalty, reason_codes)

        trust_score  — Decimal, 4dp, ∈ [0.0000, 1.0000]
        s_merchant   — Decimal, 4dp, ∈ [0.0000, 1.0000]
        m_penalty    — Decimal, 2dp, ∈ {1.00, 1.50, 2.00}
        reason_codes — list of canonical REP_* strings
    """
    # Import here to avoid module-level circular dependency issues
    # (contracts.py may not be on PYTHONPATH in all test environments)
    total = Decimal(str(rep.total_orders))
    successful = Decimal(str(rep.successful_orders))
    fraud = rep.verified_fraud_claims

    # Bayesian trust: (successful + 7) / (total + 10)
    numerator = successful + _PRIOR_MC
    denominator = total + _PRIOR_M
    if denominator == Decimal("0"):
        trust_score = _PRIOR_C  # degenerate case: use prior
    else:
        trust_score = _quantize4(_clamp(numerator / denominator))

    s_merchant = _quantize4(_clamp(Decimal("1.0000") - trust_score))

    # Fraud penalty
    if fraud == 0:
        m_penalty = _PENALTY_ZERO
        reason_codes: List[str] = [_REASON_VERIFIED]
    elif fraud == 1:
        m_penalty = _PENALTY_ONE
        reason_codes = [_REASON_FLAGGED]
    else:
        m_penalty = _PENALTY_TWO_PLUS
        reason_codes = [_REASON_FLAGGED]

    return (trust_score, s_merchant, m_penalty, reason_codes)
