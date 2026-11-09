"""Deterministic demo mode cache — 3 seeded scenarios (Person 2).

Returns pre-computed PillarScores and RiskAssessment for the three
canonical demo merchants defined in spec section 7.1. All values match
the spec worked examples exactly. Responses are served in < 5ms with
no network calls.

Demo merchants:
    mer_audio_den   — Trusted Seller   (r_final = 7.80,  fee = 0.47, gate = COVERAGE_AVAILABLE)
    mer_dash_deals  — AI Scam          (r_final = 70.25, fee = 2.81, gate = COVERAGE_AVAILABLE)
    mer_refurb_find — Repeat Offender  (r_final = 81.00, fee = None, gate = BLOCKED_HIGH_RISK)

Spec verification (weights: W_VIS=0.25, W_SEM=0.20, W_REV=0.15, W_PRICE=0.15, W_MER=0.25):
    Trusted : 0.25*0.05+0.20*0.10+0.15*0.10+0.15*0.10+0.25*0.0619 = 0.0780 → ×1.00×100 = 7.80 ✓
    Scam    : 0.25*0.92+0.20*0.85+0.15*0.70+0.15*0.90+0.25*0.25   = 0.7025 → ×1.00×100 = 70.25 ✓
    Offender: 0.25*0.50+0.20*0.40+0.15*0.60+0.15*0.30+0.25*0.26   = 0.4050 → ×2.00×100 = 81.00 ✓

Merchant trust derivation (Bayesian prior m=10, C=0.70):
    Trusted : total=200, succ=190, fraud=0  → (190+7)/(200+10) = 197/210 = 0.9381
    Scam    : total=2,   succ=2,   fraud=0  → (2+7)/(2+10)     = 9/12    = 0.7500
    Offender: total=40,  succ=30,  fraud=2  → (30+7)/(40+10)   = 37/50   = 0.7400

Environment:
    SHIELD_DEMO_MODE=1  — enables demo fast-path for all requests.
"""

from __future__ import annotations

import os
from decimal import Decimal
from typing import Optional, Dict, Any

# ---------------------------------------------------------------------------
# Sentinel set: all known demo merchant IDs and item hash mappings
# ---------------------------------------------------------------------------
_DEMO_MERCHANT_IDS: frozenset[str] = frozenset(
    {"mer_audio_den", "mer_dash_deals", "mer_refurb_find"}
)

_DEMO_HASH_TO_MERCHANT: Dict[str, str] = {
    "b3a9e55dde37b80d97fa8713a01dda91d3c1c7f2e33bd0cf8a35f4f4e3a11c05": "mer_audio_den",
    "9d40b7a2c02df11bff6f398ba9dfde04c8f2fba666561e4b7a4d9b4c4f00ad21": "mer_dash_deals",
    "4c8d2ca1d5d63a1eb9b60a15b4a1f8bff38dcdbf7a8c3d10b8dcfc8d3a417bae": "mer_refurb_find",
    "trusted": "mer_audio_den",
    "scam": "mer_dash_deals",
    "offender": "mer_refurb_find",
}


def is_demo_mode(identifier: Optional[str] = None) -> bool:
    """Return True if demo mode is active.

    When called with no args: returns True if DEMO_DETERMINISTIC_MODE or SHIELD_DEMO_MODE env var is set.
    When called with a merchant_id or item_hash: returns True if that identifier is a demo scenario.
    """
    if identifier is not None:
        mer_id = _DEMO_HASH_TO_MERCHANT.get(identifier, identifier)
        return mer_id in _DEMO_MERCHANT_IDS
    val = os.environ.get("DEMO_DETERMINISTIC_MODE") or os.environ.get("SHIELD_DEMO_MODE", "")
    return val.strip().lower() in ("1", "true", "yes")


# ---------------------------------------------------------------------------
# Pre-computed pillar scores (spec section 7.1)
# All values are exact Decimal — no floats.
# ---------------------------------------------------------------------------
_DEMO_PILLARS_RAW: Dict[str, Dict[str, Any]] = {
    # Scenario 1: Low-risk / Trusted Seller
    # trust = (190+7)/(200+10) = 0.9381 → s_merchant = 0.0619
    # r_base = 0.25*0.0500+0.20*0.1000+0.15*0.1000+0.15*0.1000+0.25*0.0619 = 0.0780
    # r_final = 0.0780 * 1.00 * 100 = 7.80
    "mer_audio_den": {
        "s_visual":     Decimal("0.0500"),
        "s_semantic":   Decimal("0.1000"),
        "s_reviews":    Decimal("0.1000"),
        "s_price":      Decimal("0.1000"),
        "is_ai_generated": False,
        "reason_codes": ["REP_VERIFIED_HISTORY"],
        "model_versions": {"visual": "heuristic-v1", "semantic": "clip-ViT-B-32"},
        "cache_status": "DEMO",
        "trust_score":  Decimal("0.9381"),
        "s_merchant":   Decimal("0.0619"),
        "m_penalty":    Decimal("1.00"),
        "r_base":       Decimal("0.0780"),
        "r_final":      Decimal("7.80"),
        "gate":         "COVERAGE_AVAILABLE",
    },
    # Scenario 2: High-risk AI-generated Scam
    # trust = (2+7)/(2+10) = 0.7500 → s_merchant = 0.2500
    # r_base = 0.25*0.9200+0.20*0.8500+0.15*0.7000+0.15*0.9000+0.25*0.2500 = 0.7025
    # r_final = 0.7025 * 1.00 * 100 = 70.25
    "mer_dash_deals": {
        "s_visual":     Decimal("0.9200"),
        "s_semantic":   Decimal("0.8500"),
        "s_reviews":    Decimal("0.7000"),
        "s_price":      Decimal("0.9000"),
        "is_ai_generated": True,
        "reason_codes": [
            "VIS_AI_GENERATED", "SEM_LOW_ALIGNMENT",
            "REV_KEYWORD_KNOCKOFF", "PRICE_EXTREME_UNDER",
        ],
        "model_versions": {"visual": "heuristic-v1", "semantic": "clip-ViT-B-32"},
        "cache_status": "DEMO",
        "trust_score":  Decimal("0.7500"),
        "s_merchant":   Decimal("0.2500"),
        "m_penalty":    Decimal("1.00"),
        "r_base":       Decimal("0.7025"),
        "r_final":      Decimal("70.25"),
        "gate":         "COVERAGE_AVAILABLE",
    },
    # Scenario 3: Repeat Offender with prior fraud claims
    # trust = (30+7)/(40+10) = 0.7400 → s_merchant = 0.2600
    # r_base = 0.25*0.5000+0.20*0.4000+0.15*0.6000+0.15*0.3000+0.25*0.2600 = 0.4050
    # r_final = 0.4050 * 2.00 * 100 = 81.00
    "mer_refurb_find": {
        "s_visual":     Decimal("0.5000"),
        "s_semantic":   Decimal("0.4000"),
        "s_reviews":    Decimal("0.6000"),
        "s_price":      Decimal("0.3000"),
        "is_ai_generated": False,
        "reason_codes": ["REP_FLAGGED_HISTORY"],
        "model_versions": {"visual": "heuristic-v1", "semantic": "clip-ViT-B-32"},
        "cache_status": "DEMO",
        "trust_score":  Decimal("0.7400"),
        "s_merchant":   Decimal("0.2600"),
        "m_penalty":    Decimal("2.00"),
        "r_base":       Decimal("0.4050"),
        "r_final":      Decimal("81.00"),
        "gate":         "BLOCKED_HIGH_RISK",
    },
}


def get_demo_pillars(identifier: str) -> Optional[Any]:
    """Return a PillarScores object for a demo merchant or item_hash, or None.

    Returns a fully constructed PillarScores (Pydantic model) when pydantic
    is available, or a plain dict in test environments without it.
    """
    mer_id = _DEMO_HASH_TO_MERCHANT.get(identifier, identifier)
    raw = _DEMO_PILLARS_RAW.get(mer_id)
    if raw is None:
        return None

    try:
        from app.schemas.contracts import PillarScores, CacheStatus
        return PillarScores(
            s_visual=raw["s_visual"],
            s_semantic=raw["s_semantic"],
            s_reviews=raw["s_reviews"],
            s_price=raw["s_price"],
            is_ai_generated=raw["is_ai_generated"],
            reason_codes=list(raw["reason_codes"]),
            model_versions=dict(raw["model_versions"]),
            cache_status=CacheStatus.DEMO,
        )
    except ImportError:
        return dict(raw)


def get_demo_pillars_raw(identifier: str) -> Optional[Dict[str, Any]]:
    """Return the raw Decimal dict for a demo merchant (no pydantic required)."""
    mer_id = _DEMO_HASH_TO_MERCHANT.get(identifier, identifier)
    data = _DEMO_PILLARS_RAW.get(mer_id)
    return dict(data) if data is not None else None


def get_demo_assessment(identifier: str, item_hash: str) -> Optional[Dict[str, Any]]:
    """Return a complete pre-computed RiskAssessment dict for a demo merchant.

    Returns None if identifier is not a demo scenario.
    All monetary and risk values are Decimal — no floats.
    """
    mer_id = _DEMO_HASH_TO_MERCHANT.get(identifier, identifier)
    data = _DEMO_PILLARS_RAW.get(mer_id)
    if data is None:
        return None

    pillars_dict = {
        "s_visual":        data["s_visual"],
        "s_semantic":      data["s_semantic"],
        "s_reviews":       data["s_reviews"],
        "s_price":         data["s_price"],
        "is_ai_generated": data["is_ai_generated"],
        "reason_codes":    list(data["reason_codes"]),
        "model_versions":  dict(data["model_versions"]),
        "cache_status":    data["cache_status"],
    }

    return {
        "item_hash":   item_hash,
        "merchant_id": merchant_id,
        "pillars":     pillars_dict,
        "trust_score": data["trust_score"],
        "s_merchant":  data["s_merchant"],
        "r_base":      data["r_base"],
        "m_penalty":   data["m_penalty"],
        "r_final":     data["r_final"],
        "gate":        data["gate"],
    }


def list_demo_merchants() -> list[str]:
    """Return the sorted list of demo merchant IDs."""
    return sorted(_DEMO_MERCHANT_IDS)
