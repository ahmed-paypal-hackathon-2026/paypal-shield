"""Pillar 4 — Robust Price Z-Score Anomaly Detection (Person 2).

Uses log-transformed prices and Median Absolute Deviation (MAD) to
produce a robust Z-score, clamped to [0.00, 1.00].

Math (spec section 6.4):
    x      = ln(P)
    x̃      = Median(ln(P_cat))
    MAD    = max(0.05, Median(|ln(P_cat) - x̃|))
    Z      = 0.6745 * (ln(P) - x̃) / MAD
    Z >= -1.50  → S_price = 0.00
    Z <  -1.50  → S_price = min(1.0, (|Z| - 1.50) / 3.00)

Extreme under-pricing → PRICE_EXTREME_UNDER reason code.
"""

from __future__ import annotations

import math
import statistics
from decimal import Decimal, ROUND_HALF_UP
from typing import List, Tuple

_TWO_PLACES = Decimal("0.01")
_CLAMP_MIN = Decimal("0.00")
_CLAMP_MAX = Decimal("1.00")
_MAD_FLOOR = 0.05
_QNORM_75 = 0.6745   # Φ^{-1}(0.75): consistent MAD scaling constant
_Z_THRESHOLD = -1.50
_Z_SCALE = 3.00
_REASON_EXTREME_UNDER = "PRICE_EXTREME_UNDER"


def _clamp(value: Decimal) -> Decimal:
    return max(_CLAMP_MIN, min(_CLAMP_MAX, value))


def _quantize(value: Decimal) -> Decimal:
    return value.quantize(_TWO_PLACES, rounding=ROUND_HALF_UP)


# ---------------------------------------------------------------------------
# Hardcoded category reference price distributions (USD, median ± spread)
# These serve as defaults when no live category_prices are provided.
# ---------------------------------------------------------------------------
_DEFAULT_CATEGORY_PRICES: dict[str, List[float]] = {
    "electronics": [49.99, 79.99, 99.99, 149.99, 199.99, 249.99, 299.99, 399.99],
    "clothing": [9.99, 14.99, 24.99, 34.99, 49.99, 79.99, 99.99],
    "home": [12.99, 19.99, 29.99, 49.99, 79.99, 99.99, 149.99],
    "toys": [7.99, 14.99, 24.99, 39.99, 59.99, 89.99],
    "sports": [9.99, 19.99, 34.99, 59.99, 99.99, 149.99, 199.99],
    "beauty": [4.99, 9.99, 19.99, 29.99, 49.99, 79.99],
    "books": [4.99, 9.99, 14.99, 24.99, 34.99],
    "automotive": [9.99, 24.99, 49.99, 99.99, 199.99, 499.99],
    "default": [9.99, 19.99, 34.99, 49.99, 99.99, 149.99, 199.99, 299.99],
}


def _median(values: List[float]) -> float:
    return statistics.median(values)


def _mad(values: List[float], median_val: float) -> float:
    """Median Absolute Deviation."""
    deviations = [abs(v - median_val) for v in values]
    return statistics.median(deviations)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def score_price(
    item_price: float | Decimal,
    category_prices: List[float] | None = None,
    category: str = "default",
) -> Tuple[Decimal, List[str]]:
    """Compute robust Z-score price risk score.

    Args:
        item_price: The listed price of the item.
        category_prices: Reference prices for the category. Uses built-in
                         defaults if None or empty.
        category: Category key for default price lookup.

    Returns:
        (s_price, reason_codes)
        s_price ∈ [0.00, 1.00] as Decimal.
        reason_codes: [PRICE_EXTREME_UNDER] or [].
    """
    price = float(item_price)
    if price <= 0:
        # Zero or negative price is maximally suspicious
        return (Decimal("1.00"), [_REASON_EXTREME_UNDER])

    ref_prices = category_prices if category_prices else None
    if not ref_prices:
        ref_prices = _DEFAULT_CATEGORY_PRICES.get(
            category, _DEFAULT_CATEGORY_PRICES["default"]
        )

    # Log-transform
    try:
        x = math.log(price)
        log_prices = [math.log(p) for p in ref_prices if p > 0]
    except (ValueError, ZeroDivisionError):
        return (_CLAMP_MAX, [_REASON_EXTREME_UNDER])

    if not log_prices:
        return (_CLAMP_MIN, [])

    x_tilde = _median(log_prices)
    mad_val = max(_MAD_FLOOR, _mad(log_prices, x_tilde))

    if mad_val == 0:
        return (_CLAMP_MIN, [])

    # Z = 0.6745 * (ln(P) - x̃) / MAD  (spec section 6.4)
    z = _QNORM_75 * (x - x_tilde) / mad_val

    reason_codes: List[str] = []
    if z >= _Z_THRESHOLD:
        s_price = _CLAMP_MIN
    else:
        # S_price = min(1.0, (|Z| - 1.50) / 3.00)
        raw = (abs(z) - 1.50) / _Z_SCALE
        s_price = _clamp(_quantize(Decimal(str(round(raw, 4)))))
        if s_price > _CLAMP_MIN:
            reason_codes.append(_REASON_EXTREME_UNDER)

    return (s_price, reason_codes)
