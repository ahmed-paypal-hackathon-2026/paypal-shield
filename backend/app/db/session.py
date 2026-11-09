"""Async SQLAlchemy engine/session factory (Person 1).

- PostgreSQL in deployment, SQLite (WAL) locally; schema stays portable.
- SQLite gets pragmas: WAL journal, busy_timeout, foreign_keys ON.
- ``get_session`` is the FastAPI dependency; commits on success and rolls
  back on error so payment paths never leave half-written transactions.
"""

from __future__ import annotations

import logging
from typing import AsyncIterator

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.db.models import *  # noqa: F401,F403 - imported so metadata is populated
from app.db.models import Base as _ModelsBase

logger = logging.getLogger(__name__)

_make_sessionmaker = async_sessionmaker


def create_database_engine(url: str, echo: bool = False) -> AsyncEngine:
    """Create an async engine with dialect-appropriate tuning."""
    if url.startswith("sqlite"):
        engine = create_async_engine(
            url,
            echo=echo,
            connect_args={"check_same_thread": False, "timeout": 30},
        )
        _register_sqlite_pragmas(engine)
    else:
        engine = create_async_engine(url, echo=echo, pool_pre_ping=True)
    return engine


def _register_sqlite_pragmas(async_engine: AsyncEngine) -> None:
    @event.listens_for(async_engine.sync_engine, "connect")
    def _set_sqlite_pragma(dbapi_connection, _record) -> None:  # pragma: no cover
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


def make_session_factory(engine: AsyncEngine) -> async_sessionmaker:
    """Session factory: expire-on-commit off so BackgroundTasks can read state."""
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


CANONICAL_DEMO_MERCHANTS = [
    {
        "merchant_id": "mer_audio_den",
        "total_orders": 200,
        "successful_orders": 190,
        "verified_fraud_claims": 0,
    },
    {
        "merchant_id": "mer_dash_deals",
        "total_orders": 2,
        "successful_orders": 2,
        "verified_fraud_claims": 0,
    },
    {
        "merchant_id": "mer_refurb_find",
        "total_orders": 40,
        "successful_orders": 30,
        "verified_fraud_claims": 2,
    },
]

DEMO_RISK_CACHE_SEEDS = [
    {
        "merchant_id": "mer_audio_den",
        "image_hash": "b3a9e55dde37b80d97fa8713a01dda91d3c1c7f2e33bd0cf8a35f4f4e3a11c05",
        "titles": ["AudioMax Studio Headphones", "Trusted merchant", "Demo Headphones", ""],
        "pillars": {
            "s_visual": "0.0500",
            "s_semantic": "0.1000",
            "s_reviews": "0.1000",
            "s_price": "0.1000",
            "is_ai_generated": False,
            "reason_codes": ["REP_VERIFIED_HISTORY"],
            "model_versions": {"visual": "heuristic-v1", "semantic": "clip-ViT-B-32"},
            "cache_status": "HIT",
        },
    },
    {
        "merchant_id": "mer_dash_deals",
        "image_hash": "9d40b7a2c02df11bff6f398ba9dfde04c8f2fba666561e4b7a4d9b4c4f00ad21",
        "titles": ["AuraGlow 8K Camera Bundle", "AI-generated scam listing", "Demo Camera", ""],
        "pillars": {
            "s_visual": "0.9200",
            "s_semantic": "0.8500",
            "s_reviews": "0.7000",
            "s_price": "0.9000",
            "is_ai_generated": True,
            "reason_codes": ["VIS_AI_GENERATED", "SEM_LOW_ALIGNMENT", "REV_KEYWORD_KNOCKOFF", "PRICE_EXTREME_UNDER"],
            "model_versions": {"visual": "heuristic-v1", "semantic": "clip-ViT-B-32"},
            "cache_status": "HIT",
        },
    },
    {
        "merchant_id": "mer_refurb_find",
        "image_hash": "4c8d2ca1d5d63a1eb9b60a15b4a1f8bff38dcdbf7a8c3d10b8dcfc8d3a417bae",
        "titles": ["RefurbOne GPU, like new", "Repeat offender", "Demo GPU", ""],
        "pillars": {
            "s_visual": "0.5000",
            "s_semantic": "0.4000",
            "s_reviews": "0.6000",
            "s_price": "0.3000",
            "is_ai_generated": False,
            "reason_codes": ["REP_FLAGGED_HISTORY"],
            "model_versions": {"visual": "heuristic-v1", "semantic": "clip-ViT-B-32"},
            "cache_status": "HIT",
        },
    },
]


async def seed_demo_merchants(engine: AsyncEngine) -> None:
    """Pre-seed canonical demo merchants, risk cache, and unbacked quotes."""
    from datetime import datetime, timedelta, timezone
    import hashlib
    from decimal import Decimal
    from app.db.models import MerchantReputationRow, RiskCacheRow, QuoteRow

    factory = make_session_factory(engine)
    async with factory() as session:
        for seed in CANONICAL_DEMO_MERCHANTS:
            existing = await session.get(MerchantReputationRow, seed["merchant_id"])
            if existing is None:
                session.add(
                    MerchantReputationRow(
                        merchant_id=seed["merchant_id"],
                        total_orders=seed["total_orders"],
                        successful_orders=seed["successful_orders"],
                        verified_fraud_claims=seed["verified_fraud_claims"],
                    )
                )
            elif existing.total_orders == 0 and existing.successful_orders == 0:
                existing.total_orders = seed["total_orders"]
                existing.successful_orders = seed["successful_orders"]
                existing.verified_fraud_claims = seed["verified_fraud_claims"]
        await session.flush()

        far_future = datetime.now(timezone.utc) + timedelta(days=3650)
        for fixture in DEMO_RISK_CACHE_SEEDS:
            img_h = fixture["image_hash"]
            for t in fixture["titles"]:
                th = hashlib.sha256(f"{t.strip().lower()} ".encode("utf-8")).hexdigest()
                existing_cache = await session.get(RiskCacheRow, (img_h, th))
                if existing_cache is None:
                    session.add(
                        RiskCacheRow(
                            image_hash=img_h,
                            text_hash=th,
                            pillars_json=fixture["pillars"],
                            model_versions=fixture["pillars"]["model_versions"],
                            expires_at=far_future,
                        )
                    )

        unbacked_quotes = [
            ("unbacked_mer_refurb_find", "mer_refurb_find", "4c8d2ca1d5d63a1eb9b60a15b4a1f8bff38dcdbf7a8c3d10b8dcfc8d3a417bae", Decimal("60.00"), Decimal("81.00")),
            ("unbacked_offender", "mer_refurb_find", "4c8d2ca1d5d63a1eb9b60a15b4a1f8bff38dcdbf7a8c3d10b8dcfc8d3a417bae", Decimal("60.00"), Decimal("81.00")),
        ]
        for qid, mer, h, price, r_fin in unbacked_quotes:
            existing_q = await session.get(QuoteRow, qid)
            if existing_q is None:
                session.add(
                    QuoteRow(
                        quote_id=qid,
                        merchant_id=mer,
                        item_hash=h,
                        item_price=price,
                        currency_code="USD",
                        protection_fee=Decimal("0.00"),
                        r_final=r_fin,
                        expires_at=far_future,
                        hmac_signature="unbacked",
                        consumed=False,
                    )
                )

        await session.commit()


async def create_all(engine: AsyncEngine) -> None:
    """Create every table from the canonical metadata (dev/test bootstrap)."""
    async with engine.begin() as conn:
        await conn.run_sync(_ModelsBase.metadata.create_all)
    await seed_demo_merchants(engine)


async def get_session(
    session_factory: async_sessionmaker,
) -> AsyncIterator[AsyncSession]:
    """Yield a session with commit-on-success, rollback-on-error."""
    async with session_factory() as session:
        try:
            yield session
            await session.commit()
            # Phase 4: publish stream events staged during the request ONLY
            # after the commit succeeded (events reflect persisted state).
            from app.services.events import flush_session_events

            await flush_session_events(session)
        except Exception:
            await session.rollback()
            # Drop staged events: their transaction never landed.
            session.info.pop("pending_events", None)
            raise
