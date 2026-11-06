"""Deterministic fallback RiskEngine (Person 1).

This is NOT Person 2's actuarial engine and never claims to be calibrated.
It exists for two reasons:
1. ``ENGINE_IMPL=stub`` lets Person 1 build, test and demo the whole backend
   independently of everything under ``app/ai/`` (ownership boundary).
2. It is the designated fail-safe used by the quote path when any model or
   external service fails (REQ-NF-03: never a 500 at checkout).

Math follows spec sections 6.1/6.2 verbatim in ``decimal.Decimal`` with
ROUND_HALF_UP; it is pure, synchronous and unconditionally exception-free.
Constants here are spec defaults, not Person 2's calibration.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional

from app.schemas.contracts import (
    ArbiterContext,
    ArbiterDecision,
    CacheStatus,
    ClaimRecord,
    ClaimState,
    GateDecision,
    MerchantReputation,
    PillarScores,
    QuoteRequest,
    RiskAssessment,
)

D = Decimal

_FOUR_PLACES = D("0.0001")
_TWO_PLACES = D("0.01")
_WEIGHTS = {
    "visual": D("0.25"),
    "semantic": D("0.20"),
    "reviews": D("0.15"),
    "price": D("0.15"),
    "merchant": D("0.25"),
}
_PRIOR_M, _PRIOR_C = 10, D("0.70")
_GATE = D("80.00")
_MIN_FEE = {
    "USD": D("0.25"), "EUR": D("0.25"), "GBP": D("0.25"),
    "JPY": D("35"), "HUF": D("90"), "TWD": D("8"),
}
_ZERO_DECIMAL = frozenset({"JPY", "HUF", "TWD"})

SUPPORTED_CURRENCIES = frozenset(_MIN_FEE)


def _q4(v: Decimal) -> Decimal:
    return v.quantize(_FOUR_PLACES, rounding=ROUND_HALF_UP)


def _q2(v: Decimal) -> Decimal:
    return v.quantize(_TWO_PLACES, rounding=ROUND_HALF_UP)


def _quantum(currency: str) -> Decimal:
    return D(1) if currency in _ZERO_DECIMAL else D("0.01")


class StubRiskEngine:
    """Protocol-satisfying deterministic engine for dev/fallback."""

    async def precompute(
        self,
        listing_id: str,
        image_bytes: bytes,
        title: str,
        description: str,
        price: Decimal,
        category: str,
        reviews: list[str],
    ) -> PillarScores:
        # Conservative neutral defaults (spec section 6.5 miss policy).
        return PillarScores(
            s_visual=D("0.50"), s_semantic=D("0.50"),
            s_reviews=_q4(D("1") if reviews and len(reviews) >= 5 else D("0.50")),
            s_price=D("0.50"),
            is_ai_generated=False,
            reason_codes=["VIS_FALLBACK_DEFAULT", "REV_INSUFFICIENT", "PRICE_INSUFFICIENT_DATA"],
            model_versions={"stub": "service-stub-v1"},
            cache_status=CacheStatus.DEMO,
        )

    def assess(
        self, req: QuoteRequest, pillars: PillarScores, rep: MerchantReputation
    ) -> RiskAssessment:
        t = ((D(rep.successful_orders) + D(_PRIOR_M) * _PRIOR_C)
             / (D(rep.total_orders) + D(_PRIOR_M)))
        s_m = _q4(D(1) - t)
        r_base = _q4(
            _WEIGHTS["visual"] * _q4(pillars.s_visual)
            + _WEIGHTS["semantic"] * _q4(pillars.s_semantic)
            + _WEIGHTS["reviews"] * _q4(pillars.s_reviews)
            + _WEIGHTS["price"] * _q4(pillars.s_price)
            + _WEIGHTS["merchant"] * s_m
        )
        penalty = (
            D("1.00") if rep.verified_fraud_claims == 0
            else D("1.50") if rep.verified_fraud_claims == 1
            else D("2.00")
        )
        r_final = _q2(min(D("100.00"), r_base * penalty * D(100)))
        if req.currency_code not in SUPPORTED_CURRENCIES:
            gate = GateDecision.UNSUPPORTED_CURRENCY
        elif r_final > _GATE:
            gate = GateDecision.BLOCKED_HIGH_RISK
        else:
            gate = GateDecision.COVERAGE_AVAILABLE
        return RiskAssessment(
            item_hash=req.item_hash,
            merchant_id=req.merchant_id,
            pillars=pillars,
            trust_score=_q4(t),
            s_merchant=s_m,
            r_base=r_base,
            m_penalty=penalty,
            r_final=r_final,
            gate=gate,
        )

    def price_fee(self, a: RiskAssessment, item_price: Decimal, currency: str) -> Decimal:
        if a.gate != GateDecision.COVERAGE_AVAILABLE or currency not in _MIN_FEE:
            return D("0.00")
        floor = _MIN_FEE[currency]
        raw = max(floor, min(item_price * D("0.04"), item_price * a.r_final / D(100) * D("0.05")))
        fee = raw.quantize(_quantum(currency), rounding=ROUND_HALF_UP)
        if fee >= item_price.quantize(_quantum(currency), rounding=ROUND_HALF_UP):
            return D("0.00")  # decline coverage: fee would reach the item price
        return fee

    async def arbitrate(
        self,
        claim: ClaimRecord,
        listing_image: bytes,
        received_image: bytes,
        ctx: ArbiterContext,
    ) -> ArbiterDecision:
        # No models here: a stub arbiter only ever escalates to MANUAL_REVIEW.
        return ArbiterDecision(
            claim_id=claim.claim_id,
            discrepancy_score=D("0.5000"),
            gate_amount_ok=ctx.claim_amount > 0,
            gate_velocity_ok=True,
            gate_no_dispute=not ctx.dispute_active,
            gate_discrepancy_ok=False,
            outcome=ClaimState.MANUAL_REVIEW,
            reason_codes=["STUB_ARBITER_MANUAL"],
        )
