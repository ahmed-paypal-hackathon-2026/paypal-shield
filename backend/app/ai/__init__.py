"""Person 2 – AI Pipeline & Actuarial Engine public interface.

Exposes a concrete ``ShieldRiskEngine`` that satisfies the ``RiskEngine``
Protocol defined in ``app.schemas.contracts``.  Person 1's routers should
import and instantiate ``engine`` (the module-level singleton).

Boundary note
─────────────
This module is the *only* entry-point into ``backend/app/ai/`` for code
outside this package.  Do not import internal pillar modules directly from
Person 1's service layer.
"""

from __future__ import annotations

import hashlib
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional
import uuid

from app.schemas.contracts import (
    ArbiterContext,
    ArbiterDecision,
    CacheStatus,
    ClaimRecord,
    GateDecision,
    MerchantReputation,
    PillarScores,
    ProtectionQuote,
    QuoteRequest,
    RiskAssessment,
)

from app.ai.cache import image_hash, pillar_cache
from app.ai.demo_cache import get_demo_pillars, get_demo_assessment, is_demo_mode
from app.ai.reputation import score_reputation
from app.ai.engine.actuarial import compute_risk, compute_fee, sign_quote
from app.ai.arbiter import arbitrate_claim

# Lazy imports for the heavy pillar scorers (avoid import-time model loading)
_visual_module: Optional[object] = None
_semantic_module: Optional[object] = None
_reviews_module: Optional[object] = None
_price_module: Optional[object] = None


def _get_visual():
    global _visual_module
    if _visual_module is None:
        from app.ai import visual as m
        _visual_module = m
    return _visual_module


def _get_semantic():
    global _semantic_module
    if _semantic_module is None:
        from app.ai import clip_semantic as m
        _semantic_module = m
    return _semantic_module


def _get_reviews():
    global _reviews_module
    if _reviews_module is None:
        from app.ai import reviews as m
        _reviews_module = m
    return _reviews_module


def _get_price():
    global _price_module
    if _price_module is None:
        from app.ai import price_zscore as m
        _price_module = m
    return _price_module


class ShieldRiskEngine:
    """Concrete implementation of the ``RiskEngine`` Protocol.

    Satisfies every method signature in ``app.schemas.contracts.RiskEngine``.
    """

    # ------------------------------------------------------------------
    # Precompute: run all 4 pillar scorers, cache result
    # ------------------------------------------------------------------
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
        """Score all pillars and cache by image SHA-256.

        Returns cached result immediately if available (CacheStatus.HIT).
        In DEMO_DETERMINISTIC_MODE, skips all model inference and returns
        the seeded demo scores (CacheStatus.DEMO).
        """
        img_h = image_hash(image_bytes)
        text_norm = f"{title.strip().lower()} {description.strip().lower()}"
        text_h = hashlib.sha256(text_norm.encode("utf-8")).hexdigest()
        key = f"{img_h}_{text_h}"

        # --- demo fast-path --------------------------------------------
        if is_demo_mode():
            # listing_id may be the merchant_id in demo scenarios
            demo = get_demo_pillars(listing_id)
            if demo is not None:
                return demo

        # --- cache hit -------------------------------------------------
        cached = await pillar_cache.get(key)
        if cached is not None:
            return cached

        # --- compute each pillar in parallel using asyncio.gather ------
        import asyncio

        vis_mod = _get_visual()
        sem_mod = _get_semantic()
        rev_mod = _get_reviews()
        price_mod = _get_price()

        # Visual and semantic are async; reviews and price are sync
        (s_vis, is_ai, vis_reason), (s_sem, sem_reason) = await asyncio.gather(
            vis_mod.score_visual(image_bytes),
            sem_mod.score_semantic(image_bytes, title, description),
        )

        s_rev, rev_codes = rev_mod.score_reviews(reviews)
        s_price, price_codes = price_mod.score_price(price, [], category)

        reason_codes: list[str] = []
        if is_ai:
            reason_codes.append("VIS_AI_GENERATED")
        elif vis_reason == "VIS_FALLBACK_DEFAULT":
            reason_codes.append("VIS_FALLBACK_DEFAULT")
        reason_codes.extend(rev_codes)
        reason_codes.extend(price_codes)
        # SEM_LOW_ALIGNMENT added by semantic module but consolidate here
        if s_sem > Decimal("0.30") and "SEM_LOW_ALIGNMENT" not in reason_codes:
            reason_codes.append("SEM_LOW_ALIGNMENT")

        pillars = PillarScores(
            s_visual=s_vis,
            s_semantic=s_sem,
            s_reviews=s_rev,
            s_price=s_price,
            is_ai_generated=is_ai,
            reason_codes=list(dict.fromkeys(reason_codes)),  # deduplicate, preserve order
            model_versions={"visual": "heuristic-v1", "semantic": "clip-ViT-B-32"},
            cache_status=CacheStatus.COMPUTED,
        )

        await pillar_cache.set(key, pillars)
        return pillars

    # ------------------------------------------------------------------
    # Assess: actuarial math — no I/O, pure Decimal
    # ------------------------------------------------------------------
    def assess(
        self,
        req: QuoteRequest,
        pillars: PillarScores,
        rep: MerchantReputation,
    ) -> RiskAssessment:
        """Convert pillar scores + merchant reputation into a RiskAssessment."""
        trust_score, s_merchant, m_penalty, rep_codes = score_reputation(rep)

        # Merge reputation reason codes into pillars (non-destructive copy)
        merged_codes = list(pillars.reason_codes)
        for rc in rep_codes:
            if rc not in merged_codes:
                merged_codes.append(rc)

        enriched_pillars = pillars.model_copy(update={"reason_codes": merged_codes})

        return compute_risk(
            pillars=enriched_pillars,
            trust_score=trust_score,
            s_merchant=s_merchant,
            m_penalty=m_penalty,
            req=req,
        )

    # ------------------------------------------------------------------
    # Price fee: currency-aware Decimal fee computation
    # ------------------------------------------------------------------
    def price_fee(
        self,
        a: RiskAssessment,
        item_price: Decimal,
        currency: str,
    ) -> Decimal:
        """Return the Buyer Protection Fee for a given assessment."""
        return compute_fee(a, item_price, currency)

    # ------------------------------------------------------------------
    # Arbitrate: 4-gate deterministic claim payout decision
    # ------------------------------------------------------------------
    async def arbitrate(
        self,
        claim: ClaimRecord,
        listing_image: bytes,
        received_image: bytes,
        ctx: ArbiterContext,
    ) -> ArbiterDecision:
        """Run claim arbitration. Returns ArbiterDecision."""
        return await arbitrate_claim(claim, listing_image, received_image, ctx)

    # ------------------------------------------------------------------
    # Convenience: build a signed ProtectionQuote
    # ------------------------------------------------------------------
    def build_quote(
        self,
        assessment: RiskAssessment,
        req: QuoteRequest,
        hmac_secret: str,
        ttl_seconds: int = 600,
    ) -> Optional[ProtectionQuote]:
        """Build and HMAC-sign a ProtectionQuote. Returns None if blocked."""
        if assessment.gate == GateDecision.BLOCKED_HIGH_RISK:
            return None

        from app.services.quotes import canonical_quote_string, sign_canonical

        fee = self.price_fee(assessment, req.item_price, req.currency_code)
        quote_id = f"q_{uuid.uuid4().hex[:16]}"
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)

        canonical = canonical_quote_string(
            quote_id=quote_id,
            merchant_id=req.merchant_id,
            item_hash=req.item_hash,
            item_price=req.item_price,
            currency_code=req.currency_code,
            protection_fee=fee,
            expires_at=expires_at,
        )
        sig = sign_canonical(canonical, [hmac_secret])

        return ProtectionQuote(
            quote_id=quote_id,
            merchant_id=req.merchant_id,
            item_hash=req.item_hash,
            item_price=req.item_price,
            currency_code=req.currency_code,
            protection_fee=fee,
            expires_at=expires_at,
            hmac_signature=sig,
        )


# Module-level singleton — Person 1 imports this
engine = ShieldRiskEngine()

__all__ = [
    "ShieldRiskEngine",
    "engine",
]
