"""Pillar 3 — Review Authenticity & Counterfeit Mining (Person 2).

Synchronous scoring of product review text for:
  - Bot-like syntactic repetition (3-gram overlap / low TTR)
  - Counterfeit keyword mining (strict deterministic lexicon)

Outputs S_reviews ∈ [0.00, 1.00] (Decimal) and canonical reason codes.

Special cases:
  - < 5 reviews → neutral score (0.50) + REV_INSUFFICIENT
"""

from __future__ import annotations

import re
from decimal import Decimal, ROUND_HALF_UP
from typing import List, Tuple

_TWO_PLACES = Decimal("0.01")
_CLAMP_MIN = Decimal("0.00")
_CLAMP_MAX = Decimal("1.00")
_NEUTRAL_SCORE = Decimal("0.50")
_MIN_REVIEWS = 5

# ---------------------------------------------------------------------------
# Canonical counterfeit keyword lexicon (spec section 6.3)
# ---------------------------------------------------------------------------
_KNOCKOFF_KEYWORDS: List[str] = [
    "cheap plastic",
    "knockoff",
    "not as pictured",
    "fake logo",
    "scam",
    "counterfeit",
    "replica",
    "knock off",
    "poor quality",
    "misleading",
    "bait and switch",
    "not authentic",
    "bootleg",
    "fake",          # standalone "fake" — e.g. "Item is fake"
]

# Bot pattern: very short TTR (type-token ratio) threshold
_BOT_TTR_THRESHOLD = 0.35
# 3-gram repetition: if >N% of 3-grams appear in >1 review it signals bot
_BOT_TRIGRAM_THRESHOLD = 0.40
# Within-review repetition: if a single review has TTR below this → bot
_BOT_INTRA_REVIEW_TTR = 0.50


def _clamp(value: Decimal) -> Decimal:
    return max(_CLAMP_MIN, min(_CLAMP_MAX, value))


def _quantize(value: Decimal) -> Decimal:
    return value.quantize(_TWO_PLACES, rounding=ROUND_HALF_UP)


def _normalize(text: str) -> str:
    return text.lower().strip()


def _tokens(text: str) -> List[str]:
    return re.findall(r"[a-z0-9']+", _normalize(text))


def _ttr(tokens: List[str]) -> float:
    """Type-Token Ratio: unique / total tokens."""
    if not tokens:
        return 1.0
    return len(set(tokens)) / len(tokens)


def _trigrams(tokens: List[str]) -> List[Tuple[str, str, str]]:
    return [(tokens[i], tokens[i+1], tokens[i+2]) for i in range(len(tokens) - 2)]


def _detect_keywords(reviews: List[str]) -> Tuple[float, bool]:
    """Return (fraction of reviews with knockoff keywords, flag)."""
    if not reviews:
        return 0.0, False
    hits = 0
    for r in reviews:
        norm = _normalize(r)
        for kw in _KNOCKOFF_KEYWORDS:
            if kw in norm:
                hits += 1
                break
    frac = hits / len(reviews)
    return frac, hits > 0


def _detect_bot_pattern(reviews: List[str]) -> Tuple[float, bool]:
    """Detect bot-like review patterns via TTR and 3-gram cross-review overlap."""
    if not reviews:
        return 0.0, False

    low_ttr_count = 0
    all_trigrams: dict[Tuple[str, str, str], int] = {}

    for r in reviews:
        tokens = _tokens(r)
        if not tokens:
            continue
        # Within-review repetition (very low TTR = same words repeated)
        if _ttr(tokens) < _BOT_TTR_THRESHOLD:
            low_ttr_count += 1
        for tg in _trigrams(tokens):
            all_trigrams[tg] = all_trigrams.get(tg, 0) + 1

    # Fraction of trigrams that appear in multiple reviews
    if all_trigrams:
        repeated = sum(1 for c in all_trigrams.values() if c > 1)
        repeat_frac = repeated / len(all_trigrams)
    else:
        repeat_frac = 0.0

    ttr_frac = low_ttr_count / len(reviews)
    bot_score = max(ttr_frac, repeat_frac)

    # Flag as bot if either:
    # - Cross-review 3-gram overlap ≥ threshold
    # - Most reviews have very low TTR (≥40%)
    is_bot = repeat_frac >= _BOT_TRIGRAM_THRESHOLD or ttr_frac >= 0.40
    return bot_score, is_bot


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def score_reviews(reviews: List[str]) -> Tuple[Decimal, List[str]]:
    """Score review list for authenticity risk.

    Args:
        reviews: List of review text strings.

    Returns:
        (s_reviews, reason_codes)
        s_reviews ∈ [0.00, 1.00] as Decimal.
        reason_codes: subset of {REV_KEYWORD_KNOCKOFF, REV_BOT_PATTERN,
                                  REV_INSUFFICIENT}
    """
    reason_codes: List[str] = []

    if len(reviews) < _MIN_REVIEWS:
        return (_NEUTRAL_SCORE, ["REV_INSUFFICIENT"])

    kw_frac, has_knockoff = _detect_keywords(reviews)
    bot_score, is_bot = _detect_bot_pattern(reviews)

    if has_knockoff:
        reason_codes.append("REV_KEYWORD_KNOCKOFF")
    if is_bot:
        reason_codes.append("REV_BOT_PATTERN")

    # Weighted risk: keyword evidence weighted higher
    raw_score = 0.0
    if has_knockoff:
        raw_score += 0.55 * min(1.0, kw_frac * 3.0)
    if is_bot:
        raw_score += 0.45 * bot_score

    if not reason_codes:
        # Clean reviews
        s_reviews = _CLAMP_MIN
    else:
        s_reviews = _clamp(_quantize(Decimal(str(round(raw_score, 4)))))
        # Ensure non-zero when reasons are present
        if s_reviews == _CLAMP_MIN:
            s_reviews = Decimal("0.30")

    return (s_reviews, reason_codes)
