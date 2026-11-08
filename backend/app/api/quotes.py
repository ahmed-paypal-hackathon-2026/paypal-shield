"""POST /api/v1/quotes (Person 1).

Flow (spec section 3.2 step 1):
1. read cached pillar scores from the risk_cache table (or conservative
   default on miss: each uncached AI pillar = 0.50, MISS_DEFAULT);
2. read the merchant's live reputation row (pillar 5, always fresh);
3. run RiskEngine.assess -> RiskAssessment (pure Decimal math, Person 2);
4. when coverage is available, build and persist an HMAC-signed quote.

Failure policy (REQ-NF-03): any engine/model failure falls back to the
deterministic stub engine with default scores. This endpoint never returns
a 500 because of a model failure.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, get_fallback_engine, get_risk_engine, get_settings
from app.api.response_models import QuoteResponse
from app.config import Settings
from app.db.models import MerchantReputationRow, RiskCacheRow
from app.schemas.contracts import (
    CacheStatus,
    MerchantReputation,
    PillarScores,
    QuoteRequest,
    RiskAssessment,
)
from app.services.quotes import build_quote, persist_quote

logger = logging.getLogger(__name__)

router = APIRouter()

DEFAULT_PILLAR = Decimal("0.50")


def compute_text_hash(title: str, description: str = "") -> str:
    """Mirror Person 2's cache key normalization (title+description, NFKC-lite)."""
    text_norm = f"{title.strip().lower()} {description.strip().lower()}"
    return hashlib.sha256(text_norm.encode("utf-8")).hexdigest()


def default_pillars() -> PillarScores:
    """Conservative, high-risk-aware defaults on cache miss (spec 6.5)."""
    return PillarScores(
        s_visual=DEFAULT_PILLAR,
        s_semantic=DEFAULT_PILLAR,
        s_reviews=DEFAULT_PILLAR,
        s_price=DEFAULT_PILLAR,
        is_ai_generated=False,
        reason_codes=["VIS_FALLBACK_DEFAULT", "REV_INSUFFICIENT", "PRICE_INSUFFICIENT_DATA"],
        model_versions={},
        cache_status=CacheStatus.MISS_DEFAULT,
    )


async def load_cached_pillars(
    session: AsyncSession, item_hash: str, text_hash: str
) -> Optional[PillarScores]:
    row = (
        await session.execute(
            select(RiskCacheRow).where(
                RiskCacheRow.image_hash == item_hash,
                RiskCacheRow.text_hash == text_hash,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    now = datetime.now(timezone.utc)
    expires_at = row.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at <= now:
        return None  # treat expired entry as a miss
    data = dict(row.pillars_json or {})
    data.pop("cache_status", None)
    try:
        pillars = PillarScores(**data, cache_status=CacheStatus.HIT)
    except Exception:  # noqa: BLE001 - malformed cache entry is a miss, not an error
        logger.warning("malformed risk_cache entry for %s (miss)", item_hash)
        return None
    return pillars


async def _get_or_create_merchant(
    session: AsyncSession, merchant_id: str
) -> MerchantReputationRow:
    row = await session.get(MerchantReputationRow, merchant_id)
    if row is None:
        # Cold-start merchant: steamrolled to the Bayesian prior by the engine.
        row = MerchantReputationRow(
            merchant_id=merchant_id,
            total_orders=0,
            successful_orders=0,
            verified_fraud_claims=0,
        )
        session.add(row)
        await session.flush()
    return row


@router.post("/quotes", response_model=QuoteResponse)
async def create_quote(
    body: QuoteRequest,
    request: Request,
    session: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
    risk_engine=Depends(get_risk_engine),
    fallback=Depends(get_fallback_engine),
) -> QuoteResponse:
    text_hash = compute_text_hash(body.item_title)

    try:
        cached = await load_cached_pillars(session, body.item_hash, text_hash)
    except Exception:  # noqa: BLE001 - DB hiccups degrade, never 500
        logger.exception("risk_cache read failed; using default pillars")
        cached = None
    pillars = cached or default_pillars()

    rep_row = await _get_or_create_merchant(session, body.merchant_id)
    reputation = MerchantReputation(
        merchant_id=rep_row.merchant_id,
        total_orders=rep_row.total_orders,
        successful_orders=rep_row.successful_orders,
        verified_fraud_claims=rep_row.verified_fraud_claims,
    )

    try:
        assessment = risk_engine.assess(body, pillars, reputation)
    except Exception as exc:  # noqa: BLE001 - REQ-NF-03 graceful degradation
        logger.error(
            "risk engine assess failed (%s: %s); falling back to deterministic engine",
            type(exc).__name__, exc,
        )
        assessment = fallback.assess(body, pillars, reputation)

    # Deterministic persistence of r_final for the signed quote (money cannot change later).
    r_final = assessment.r_final if isinstance(assessment.r_final, Decimal) else Decimal(str(assessment.r_final))

    quote = build_quote(
        assessment,
        body,
        settings,
        risk_engine=risk_engine,
        fallback=fallback,
    )
    if quote is not None:
        try:
            persist_quote(session, quote, r_final)
        except Exception:  # noqa: BLE001
            logger.exception("failed to persist quote %s", quote.quote_id)
            await session.rollback()
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="internal_error",
            ) from None

    return QuoteResponse(assessment=assessment, quote=quote)
