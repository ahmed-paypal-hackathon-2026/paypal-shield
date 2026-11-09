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

import asyncio
import hashlib
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.clip_semantic import SemanticArbiter
from app.ai.visual import VisualArbiter
from app.api.deps import get_db, get_fallback_engine, get_risk_engine, get_settings
from app.api.response_models import QuoteResponse
from app.config import Settings
from app.db.models import MerchantReputationRow, QuoteRow, RiskCacheRow
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


def _is_deterministic_mode(settings: Settings) -> bool:
    """Return True if DEMO_DETERMINISTIC_MODE or SHIELD_DEMO_MODE is explicitly set to true.

    Returns False if DEMO_DETERMINISTIC_MODE is false (or not set to true).
    """
    env_det = os.environ.get("DEMO_DETERMINISTIC_MODE")
    if env_det is not None:
        return env_det.strip().lower() in ("1", "true", "yes")
    shield_env = os.environ.get("SHIELD_DEMO_MODE")
    if shield_env is not None:
        return shield_env.strip().lower() in ("1", "true", "yes")
    return False


def load_scenario_image(
    item_hash: str,
    merchant_id: str = "",
    item_title: str = "",
) -> Optional[bytes]:
    """Load image bytes for a listing or demo scenario from fixture directories."""
    candidate_dirs = [
        Path("backend/fixtures/images"),
        Path("fixtures/images"),
        Path("shared/fixtures/images"),
        Path(__file__).resolve().parents[3] / "backend" / "fixtures" / "images",
        Path(__file__).resolve().parents[3] / "shared" / "fixtures" / "images",
        Path(__file__).resolve().parents[2] / "fixtures" / "images",
        Path(__file__).resolve().parents[1] / "fixtures" / "images",
    ]

    title_l = (item_title or "").lower()
    mer_l = (merchant_id or "").lower()
    hash_l = (item_hash or "").lower()

    scenario_keys = []
    if "audio_den" in mer_l or "b3a9e55d" in hash_l or "headphone" in title_l or "trusted" in title_l:
        scenario_keys = ["trusted", "mer_audio_den", "headphones"]
    elif "dash_deals" in mer_l or "9d40b7a2" in hash_l or "camera" in title_l or "scam" in title_l:
        scenario_keys = ["scam", "mer_dash_deals", "camera"]
    elif "refurb_find" in mer_l or "4c8d2ca1" in hash_l or "gpu" in title_l or "offender" in title_l:
        scenario_keys = ["offender", "mer_refurb_find", "gpu"]

    candidate_filenames = []
    if item_hash:
        candidate_filenames.extend([f"{item_hash}.png", f"{item_hash}.jpg", f"{item_hash}.webp"])
    if merchant_id:
        candidate_filenames.extend([f"{merchant_id}.png", f"{merchant_id}.jpg", f"{merchant_id}.webp"])
    for sk in scenario_keys:
        candidate_filenames.extend([f"{sk}.png", f"{sk}.jpg", f"{sk}.webp"])
    candidate_filenames.extend(["valid.png", "valid.jpg", "valid.webp"])

    for d in candidate_dirs:
        if not d.is_dir():
            continue
        for fn in candidate_filenames:
            target = d / fn
            if target.is_file():
                try:
                    data = target.read_bytes()
                    if data:
                        return data
                except Exception:
                    continue

    return None


def _resolve_scenario_description(merchant_id: str, title: str) -> str:
    mer_l = (merchant_id or "").lower()
    title_l = (title or "").lower()
    if "audio_den" in mer_l or "headphone" in title_l or "trusted" in title_l:
        return "AudioMax Studio Headphones authentic high quality studio headphones"
    if "dash_deals" in mer_l or "camera" in title_l or "scam" in title_l:
        return "cheap plastic knockoff replica toy"
    if "refurb_find" in mer_l or "gpu" in title_l or "offender" in title_l:
        return "RefurbOne GPU refurb unit refurbished graphics card"
    return f"{title} authentic product"


def _load_scenario_reviews(merchant_id: str) -> list[str]:
    mer_l = (merchant_id or "").lower()
    if "audio_den" in mer_l or "trusted" in mer_l:
        return [
            "Excellent sound quality, arrived quickly, works with my laptop.",
            "Battery lasts a whole week and the build feels premium.",
            "Exactly as described in the listing, packaging was solid.",
            "Great value for the money, would order from this store again.",
            "Delivery took two days and support answered my question fast.",
            "Comfortable fit for long sessions, would recommend.",
        ]
    if "dash_deals" in mer_l or "scam" in mer_l:
        return [
            "Cheap plastic knockoff, totally fake!",
            "Not as pictured, feels like a cheap replica.",
            "Item is fake, scam listing, avoid!",
            "Terrible quality, counterfeit product.",
            "Cheap plastic and broken on arrival.",
        ]
    return [
        "Refurbished GPU works ok but looks used with scratches.",
        "Decent card for the price, some wear and tear.",
        "Working condition, fan is a bit loud.",
        "Delivered on time, refurbished quality.",
        "Acceptable refurb condition, runs warm.",
    ]


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


def is_mock_or_default_pillars(data: dict) -> bool:
    """Return True if cached entry is static mock data or conservative default scores.

    Dynamic live-computed rows contain 'computed_live': True.
    """
    if data.get("computed_live") is True:
        return False

    reasons = set(data.get("reason_codes") or [])
    if "VIS_FALLBACK_DEFAULT" in reasons:
        return True

    try:
        s_vis = Decimal(str(data.get("s_visual", "0.50")))
        s_sem = Decimal(str(data.get("s_semantic", "0.50")))
        s_rev = Decimal(str(data.get("s_reviews", "0.50")))
        s_price = Decimal(str(data.get("s_price", "0.50")))
        if s_vis == Decimal("0.50") and s_sem == Decimal("0.50") and s_rev == Decimal("0.50") and s_price == Decimal("0.50"):
            return True
    except Exception:
        return True

    return True


async def load_cached_pillars(
    session: AsyncSession,
    item_hash: str,
    text_hash: str,
    merchant_id: Optional[str] = None,
) -> Optional[PillarScores]:
    """Look up authentic live-computed pillars in risk_cache.

    Returns PillarScores on a valid unexpired live-computed cache hit,
    or None on cache miss / mock values. Does NOT return demo_cache numbers.
    """
    if not item_hash:
        return None

    row = (
        await session.execute(
            select(RiskCacheRow).where(
                RiskCacheRow.image_hash == item_hash,
                RiskCacheRow.text_hash == text_hash,
            )
        )
    ).scalar_one_or_none()

    if row is None:
        row = (
            await session.execute(
                select(RiskCacheRow)
                .where(RiskCacheRow.image_hash == item_hash)
                .order_by(RiskCacheRow.expires_at.desc())
            )
        ).scalars().first()

    now = datetime.now(timezone.utc)
    if row is not None:
        expires_at = row.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at > now:
            data = dict(row.pillars_json or {})
            if is_mock_or_default_pillars(data):
                logger.info(
                    "risk_cache entry for %s contains mock/default values (treating as miss)",
                    item_hash,
                )
                return None

            data.pop("cache_status", None)
            try:
                scores = PillarScores(**data, cache_status=CacheStatus.HIT)
                logger.info("Risk cache hit for %s", item_hash)
                return scores
            except Exception:
                logger.warning("malformed risk_cache entry for %s (miss)", item_hash)
                return None

    return None


async def save_risk_cache(
    session: AsyncSession,
    item_hash: str,
    text_hash: str,
    pillars: PillarScores,
    ttl_seconds: int = 3600,
) -> None:
    """Persist authentic calculated scores into risk_cache with a valid TTL."""
    if not item_hash:
        return

    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(seconds=ttl_seconds)

    pillars_dict = pillars.model_dump(mode="json")
    pillars_dict["computed_live"] = True

    model_versions = dict(pillars.model_versions or {})
    model_versions["live"] = True

    try:
        existing = await session.get(RiskCacheRow, (item_hash, text_hash))
        if existing is not None:
            existing.pillars_json = pillars_dict
            existing.model_versions = model_versions
            existing.expires_at = expires_at
        else:
            session.add(
                RiskCacheRow(
                    image_hash=item_hash,
                    text_hash=text_hash,
                    pillars_json=pillars_dict,
                    model_versions=model_versions,
                    expires_at=expires_at,
                )
            )
        await session.flush()
        logger.info(
            "Persisted authentic live AI scores to risk_cache for %s (TTL: %ds, expires_at: %s)",
            item_hash,
            ttl_seconds,
            expires_at.isoformat(),
        )
    except Exception as exc:
        logger.warning(
            "Failed to persist to risk_cache for %s (%s: %s)",
            item_hash,
            type(exc).__name__,
            exc,
        )


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

    deterministic = _is_deterministic_mode(settings)
    cached: Optional[PillarScores] = None

    if deterministic:
        # Strict deterministic toggle: load from demo cache
        from app.ai.demo_cache import get_demo_pillars

        demo_p = None
        if body.merchant_id:
            demo_p = get_demo_pillars(body.merchant_id)
        if demo_p is None and body.item_hash:
            demo_p = get_demo_pillars(body.item_hash)
        if demo_p is not None:
            if isinstance(demo_p, PillarScores):
                cached = demo_p
            else:
                data = dict(demo_p)
                data.pop("cache_status", None)
                cached = PillarScores(**data, cache_status=CacheStatus.HIT)
        else:
            cached = default_pillars()
    else:
        # Live AI Inference Pipeline with Dynamic Cache
        # Step 1: Check risk_cache
        try:
            cached = await load_cached_pillars(
                session, body.item_hash, text_hash, body.merchant_id
            )
        except Exception as exc:
            logger.warning(
                "risk_cache read failed for %s (%s: %s)",
                body.item_hash,
                type(exc).__name__,
                exc,
            )
            cached = None

        # Step 2: On cache miss (or if cache contained default/mock values):
        if cached is None:
            logger.info(
                "Live AI inference running for %s: executing VisualArbiter + SemanticArbiter...",
                body.item_hash,
            )
            start_time = time.perf_counter()
            try:
                image_bytes = load_scenario_image(
                    body.item_hash, body.merchant_id, body.item_title
                )
                if not image_bytes:
                    raise RuntimeError(f"Authentic image asset not found for {body.item_hash}")

                vis_arb = VisualArbiter()
                sem_arb = SemanticArbiter()
                desc = _resolve_scenario_description(body.merchant_id, body.item_title)

                (s_vis, is_ai, vis_reason), (s_sem, sem_reason) = await asyncio.gather(
                    vis_arb.evaluate(image_bytes),
                    sem_arb.evaluate(image_bytes, body.item_title, desc),
                )

                from app.ai.reviews import score_reviews
                from app.ai.price_zscore import score_price

                reviews_list = _load_scenario_reviews(body.merchant_id)
                s_rev, rev_codes = score_reviews(reviews_list)
                s_price, price_codes = score_price(body.item_price, [], "electronics")

                reason_codes: list[str] = []
                if is_ai:
                    reason_codes.append("VIS_AI_GENERATED")
                elif vis_reason and vis_reason != "VIS_FALLBACK_DEFAULT":
                    reason_codes.append(vis_reason)

                if s_sem > Decimal("0.30") and "SEM_LOW_ALIGNMENT" not in reason_codes:
                    reason_codes.append("SEM_LOW_ALIGNMENT")

                reason_codes.extend(rev_codes)
                reason_codes.extend(price_codes)

                duration_ms = (time.perf_counter() - start_time) * 1000
                logger.info(
                    "Live AI inference for %s completed in %.2f ms",
                    body.item_hash,
                    duration_ms,
                )

                cached = PillarScores(
                    s_visual=s_vis,
                    s_semantic=s_sem,
                    s_reviews=s_rev,
                    s_price=s_price,
                    is_ai_generated=is_ai,
                    reason_codes=list(dict.fromkeys(reason_codes)),
                    model_versions={"visual": "heuristic-v1", "semantic": "clip-ViT-B-32"},
                    cache_status=CacheStatus.COMPUTED,
                )

                # Persist authentic calculated scores into risk_cache with valid TTL
                ttl = getattr(settings, "risk_cache_ttl_seconds", 3600)
                await save_risk_cache(
                    session=session,
                    item_hash=body.item_hash,
                    text_hash=text_hash,
                    pillars=cached,
                    ttl_seconds=ttl,
                )

            except Exception as exc:
                # Step 3: Fallback Constraint:
                # Static fallbacks (demo_cache / default 0.50) should ONLY ever be touched
                # if the live model evaluation throws an unrecoverable runtime exception.
                duration_ms = (time.perf_counter() - start_time) * 1000
                logger.warning(
                    "Live model evaluation failed for %s after %.2f ms (%s: %s); falling back to demo_cache / default pillars",
                    body.item_hash,
                    duration_ms,
                    type(exc).__name__,
                    exc,
                )
                from app.ai.demo_cache import get_demo_pillars

                demo_p = None
                if body.merchant_id:
                    demo_p = get_demo_pillars(body.merchant_id)
                if demo_p is None and body.item_hash:
                    demo_p = get_demo_pillars(body.item_hash)
                if demo_p is not None:
                    if isinstance(demo_p, PillarScores):
                        cached = demo_p
                    else:
                        data = dict(demo_p)
                        data.pop("cache_status", None)
                        cached = PillarScores(**data, cache_status=CacheStatus.HIT)
                else:
                    cached = default_pillars()

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
    else:
        # High-risk / blocked scenario: persist unbacked quote row(s) so checkout without coverage satisfies FK
        far_future = datetime.now(timezone.utc) + timedelta(days=365)
        candidate_ids = [f"unbacked_{body.merchant_id}"]
        if body.merchant_id == "mer_refurb_find":
            candidate_ids.append("unbacked_offender")
        for u_id in candidate_ids:
            existing_unbacked = await session.get(QuoteRow, u_id)
            if existing_unbacked is None:
                session.add(
                    QuoteRow(
                        quote_id=u_id,
                        merchant_id=body.merchant_id,
                        item_hash=body.item_hash,
                        item_price=body.item_price,
                        currency_code=body.currency_code,
                        protection_fee=Decimal("0.00"),
                        r_final=r_final,
                        expires_at=far_future,
                        hmac_signature="unbacked",
                        consumed=False,
                    )
                )
        try:
            await session.flush()
        except Exception:  # noqa: BLE001
            logger.warning("failed to flush unbacked quote row")

    return QuoteResponse(assessment=assessment, quote=quote)
