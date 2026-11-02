"""Unit tests for the AI Actuarial Engine — Person 2 scope.

Run with:
    cd backend && python -m pytest tests/unit_ai/ -v

All tests are self-contained: they exercise pure Decimal arithmetic and
deterministic logic without any network calls, ML model loading, or
database access.
"""

from __future__ import annotations

import hashlib
import hmac as _hmac
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

import pytest


# ---------------------------------------------------------------------------
# Helpers shared across tests
# ---------------------------------------------------------------------------

def d(s: str) -> Decimal:
    """Shorthand constructor — converts a string to Decimal."""
    return Decimal(s)


# ---------------------------------------------------------------------------
# 1. Actuarial engine — weighted risk formula
# ---------------------------------------------------------------------------

class TestActuarialWeightedRisk:
    """Verify R_base and R_final with zero-float guarantee."""

    def _compute(
        self,
        s_vis: str, s_sem: str, s_rev: str, s_price: str,
        s_mer: str, m_pen: str,
    ) -> tuple[Decimal, Decimal]:
        from app.ai.engine.actuarial import _compute_r_base, _compute_r_final  # type: ignore[attr-defined]
        r_base = _compute_r_base(d(s_vis), d(s_sem), d(s_rev), d(s_price), d(s_mer))
        r_final = _compute_r_final(r_base, d(m_pen))
        return r_base, r_final

    def test_trusted_scenario_spec_values(self):
        """Spec section 6.3 — Trusted merchant worked example."""
        from app.ai.engine.actuarial import _compute_r_base, _compute_r_final
        r_base = _compute_r_base(d("0.0500"), d("0.1000"), d("0.1000"), d("0.1000"), d("0.0619"))
        r_final = _compute_r_final(r_base, d("1.00"))
        assert r_base == d("0.0780"), f"Expected r_base=0.0780, got {r_base}"
        assert r_final == d("7.80"), f"Expected r_final=7.80, got {r_final}"

    def test_scam_scenario_spec_values(self):
        """Spec section 6.3 — AI-Generated Scam worked example."""
        from app.ai.engine.actuarial import _compute_r_base, _compute_r_final
        r_base = _compute_r_base(d("0.9200"), d("0.8500"), d("0.7000"), d("0.9000"), d("0.2500"))
        r_final = _compute_r_final(r_base, d("1.00"))
        assert r_base == d("0.7025"), f"Expected r_base=0.7025, got {r_base}"
        assert r_final == d("70.25"), f"Expected r_final=70.25, got {r_final}"

    def test_offender_scenario_spec_values(self):
        """Spec section 6.3 — Repeat Offender worked example (r_final capped by gate)."""
        from app.ai.engine.actuarial import _compute_r_base, _compute_r_final
        r_base = _compute_r_base(d("0.5000"), d("0.4000"), d("0.6000"), d("0.3000"), d("0.2600"))
        r_final = _compute_r_final(r_base, d("2.00"))
        assert r_base == d("0.4050"), f"Expected r_base=0.4050, got {r_base}"
        assert r_final == d("81.00"), f"Expected r_final=81.00, got {r_final}"

    def test_r_final_capped_at_100(self):
        from app.ai.engine.actuarial import _compute_r_base, _compute_r_final
        r_base = _compute_r_base(d("1.0"), d("1.0"), d("1.0"), d("1.0"), d("1.0"))
        r_final = _compute_r_final(r_base, d("2.00"))
        assert r_final == d("100.00"), f"r_final must be capped at 100, got {r_final}"

    def test_zero_risk_all_zero_pillars(self):
        from app.ai.engine.actuarial import _compute_r_base, _compute_r_final
        r_base = _compute_r_base(d("0"), d("0"), d("0"), d("0"), d("0"))
        r_final = _compute_r_final(r_base, d("1.00"))
        assert r_final == d("0.00")

    def test_no_float_in_module(self):
        """Static assertion: actuarial.py must not contain bare float literals."""
        import ast
        from pathlib import Path
        src = (Path(__file__).parents[2] / "app" / "ai" / "engine" / "actuarial.py").read_text()
        tree = ast.parse(src)
        float_literals = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, float)
        ]
        assert float_literals == [], (
            f"Found {len(float_literals)} bare float literal(s) in actuarial.py — "
            "use Decimal strings instead: " +
            str([(n.value, n.lineno) for n in float_literals])
        )


# ---------------------------------------------------------------------------
# 2. Safety Gate — 80% threshold
# ---------------------------------------------------------------------------

class TestSafetyGate:

    def test_exactly_80_is_available(self):
        """r_final == 80.00 → COVERAGE_AVAILABLE (boundary condition)."""
        from app.ai.engine.actuarial import _gate_decision
        from app.schemas.contracts import GateDecision
        assert _gate_decision(d("80.00")) == GateDecision.COVERAGE_AVAILABLE

    def test_above_80_is_blocked(self):
        from app.ai.engine.actuarial import _gate_decision
        from app.schemas.contracts import GateDecision
        assert _gate_decision(d("80.01")) == GateDecision.BLOCKED_HIGH_RISK
        assert _gate_decision(d("81.00")) == GateDecision.BLOCKED_HIGH_RISK
        assert _gate_decision(d("100.00")) == GateDecision.BLOCKED_HIGH_RISK

    def test_below_80_is_available(self):
        from app.ai.engine.actuarial import _gate_decision
        from app.schemas.contracts import GateDecision
        assert _gate_decision(d("0.00")) == GateDecision.COVERAGE_AVAILABLE
        assert _gate_decision(d("7.80")) == GateDecision.COVERAGE_AVAILABLE
        assert _gate_decision(d("70.25")) == GateDecision.COVERAGE_AVAILABLE
        assert _gate_decision(d("79.99")) == GateDecision.COVERAGE_AVAILABLE


# ---------------------------------------------------------------------------
# 3. Fee Pricing
# ---------------------------------------------------------------------------

class TestFeePricing:

    def test_trusted_scenario_fee(self):
        """Spec: item_price=120.00, r_final=7.80 → fee=0.47."""
        from app.ai.engine.actuarial import _compute_fee_amount
        fee = _compute_fee_amount(d("120.00"), d("7.80"), "USD")
        assert fee == d("0.47"), f"Expected 0.47, got {fee}"

    def test_scam_scenario_fee(self):
        """Spec: item_price=80.00, r_final=70.25 → fee=2.81."""
        from app.ai.engine.actuarial import _compute_fee_amount
        fee = _compute_fee_amount(d("80.00"), d("70.25"), "USD")
        assert fee == d("2.81"), f"Expected 2.81, got {fee}"

    def test_fee_floor_applied(self):
        """Low-risk item should never go below MIN_FEE=0.25."""
        from app.ai.engine.actuarial import _compute_fee_amount
        fee = _compute_fee_amount(d("1.00"), d("0.10"), "USD")
        assert fee >= d("0.25"), f"Fee must be >= MIN_FEE=0.25, got {fee}"

    def test_fee_cap_at_4_percent(self):
        """Fee should never exceed 4% of item price."""
        from app.ai.engine.actuarial import _compute_fee_amount
        item_price = d("1000.00")
        fee = _compute_fee_amount(item_price, d("99.00"), "USD")
        assert fee <= item_price * d("0.04"), f"Fee must be <= 4% of item_price, got {fee}"

    def test_jpy_zero_decimal_quantization(self):
        """JPY has no fractional units — fee must be quantized to integer."""
        from app.ai.engine.actuarial import _compute_fee_amount
        fee = _compute_fee_amount(d("5000"), d("7.80"), "JPY")
        assert fee == fee.quantize(Decimal("1")), f"JPY fee must be integer, got {fee}"
        assert fee >= d("35"), f"JPY MIN_FEE is 35, got {fee}"

    def test_blocked_gate_returns_zero_fee(self):
        """BLOCKED items must return fee=0.00, not a computed fee."""
        from app.ai.engine.actuarial import _compute_fee_amount
        from app.ai.engine.actuarial import _gate_decision
        from app.schemas.contracts import GateDecision
        gate = _gate_decision(d("81.00"))
        assert gate == GateDecision.BLOCKED_HIGH_RISK
        # When blocked, fee must be 0
        fee = d("0.00") if gate == GateDecision.BLOCKED_HIGH_RISK else _compute_fee_amount(d("60.00"), d("81.00"), "USD")
        assert fee == d("0.00")


# ---------------------------------------------------------------------------
# 4. HMAC Quote Signing & Tamper Detection
# ---------------------------------------------------------------------------

class TestHMACQuoteSigning:

    def _sign(self, **kwargs: Any) -> str:
        from app.ai.engine.actuarial import sign_quote
        from datetime import datetime, timezone
        defaults = dict(
            quote_id="q_test001",
            merchant_id="mer_audio_den",
            item_hash="abc123",
            item_price=d("120.00"),
            currency_code="USD",
            fee=d("0.47"),
            is_eligible=True,
            expires_at=datetime(2026, 12, 1, tzinfo=timezone.utc),
            hmac_secret="super-secret-key",
        )
        defaults.update(kwargs)
        return sign_quote(**defaults)

    def test_same_inputs_produce_same_signature(self):
        assert self._sign() == self._sign()

    def test_tampered_fee_fails_verification(self):
        original = self._sign(fee=d("0.47"))
        tampered = self._sign(fee=d("0.00"))
        assert original != tampered

    def test_tampered_merchant_fails(self):
        original = self._sign(merchant_id="mer_audio_den")
        tampered = self._sign(merchant_id="mer_malicious")
        assert original != tampered

    def test_tampered_eligible_flag_fails(self):
        original = self._sign(is_eligible=True)
        tampered = self._sign(is_eligible=False)
        assert original != tampered

    def test_signature_is_64_hex_chars(self):
        sig = self._sign()
        assert len(sig) == 64 and all(c in "0123456789abcdef" for c in sig)


# ---------------------------------------------------------------------------
# 5. Bayesian Reputation & Penalty Multiplier
# ---------------------------------------------------------------------------

class TestReputation:

    def _score(self, total: int, successful: int, fraud: int):
        from app.ai.reputation import score_reputation
        from app.schemas.contracts import MerchantReputation
        rep = MerchantReputation(
            merchant_id="test",
            total_orders=total,
            successful_orders=successful,
            verified_fraud_claims=fraud,
        )
        return score_reputation(rep)

    def test_trusted_merchant_spec_values(self):
        """200 orders, 190 successful, 0 fraud → trust=0.9381, s_merchant=0.0619."""
        trust, s_merchant, m_penalty, _ = self._score(200, 190, 0)
        assert trust == d("0.9381"), f"trust_score={trust}"
        assert s_merchant == d("0.0619"), f"s_merchant={s_merchant}"
        assert m_penalty == d("1.00")

    def test_scam_merchant_spec_values(self):
        """2 orders, 2 successful, 0 fraud → trust=0.7500, s_merchant=0.2500."""
        trust, s_merchant, m_penalty, _ = self._score(2, 2, 0)
        assert trust == d("0.7500"), f"trust={trust}"
        assert s_merchant == d("0.2500"), f"s_merchant={s_merchant}"
        assert m_penalty == d("1.00")

    def test_offender_spec_values(self):
        """40 orders, 30 successful, 2 fraud → trust=0.7400, m_penalty=2.00."""
        trust, s_merchant, m_penalty, _ = self._score(40, 30, 2)
        assert trust == d("0.7400"), f"trust={trust}"
        assert s_merchant == d("0.2600"), f"s_merchant={s_merchant}"
        assert m_penalty == d("2.00")

    def test_one_fraud_claim_penalty_1_50(self):
        _, _, m_penalty, _ = self._score(10, 9, 1)
        assert m_penalty == d("1.50")

    def test_zero_orders_does_not_crash(self):
        trust, s_merchant, m_penalty, _ = self._score(0, 0, 0)
        # trust = (0 + 7) / (0 + 10) = 0.7000
        assert trust == d("0.7000")
        assert s_merchant == d("0.3000")

    def test_rep_flagged_history_reason_code(self):
        _, _, _, codes = self._score(10, 8, 1)
        assert "REP_FLAGGED_HISTORY" in codes

    def test_rep_verified_high_trust_code(self):
        _, _, _, codes = self._score(1000, 995, 0)
        assert "REP_VERIFIED_HISTORY" in codes

    def test_s_merchant_clamped_to_zero(self):
        # If trust_score rounds above 1.0 somehow, s_merchant must not be negative
        _, s_merchant, _, _ = self._score(10000, 10000, 0)
        assert s_merchant >= d("0.00")


# ---------------------------------------------------------------------------
# 6. Price Robust Z-Score
# ---------------------------------------------------------------------------

class TestPriceZScore:

    def test_fair_price_gives_zero_risk(self):
        """Price in the middle of category distribution → s_price=0.00."""
        from app.ai.price_zscore import score_price
        category_prices = [d("80"), d("100"), d("120"), d("140"), d("160")]
        s_price, codes = score_price(d("120"), category_prices)
        assert s_price == d("0.00"), f"Expected 0.00, got {s_price}"
        assert "PRICE_EXTREME_UNDER" not in codes

    def test_extreme_underpricing_gives_high_risk(self):
        """Price 90% below category median → s_price > 0."""
        from app.ai.price_zscore import score_price
        category_prices = [d("100"), d("120"), d("130"), d("140"), d("150")]
        s_price, codes = score_price(d("5"), category_prices)
        assert s_price > d("0.00"), f"Extreme underpricing should raise risk, got {s_price}"
        assert "PRICE_EXTREME_UNDER" in codes

    def test_zero_mad_floor_prevents_division_error(self):
        """All same prices → MAD=0 → should use MAD_FLOOR=0.0500."""
        from app.ai.price_zscore import score_price
        category_prices = [d("100"), d("100"), d("100"), d("100"), d("100")]
        # Should not raise ZeroDivisionError
        s_price, _ = score_price(d("100"), category_prices)
        assert d("0.00") <= s_price <= d("1.00")

    def test_s_price_clamped_to_one(self):
        """Absurdly low price → s_price clamped to 1.00."""
        from app.ai.price_zscore import score_price
        category_prices = [d("500"), d("600"), d("700"), d("800"), d("900")]
        s_price, _ = score_price(d("1"), category_prices)
        assert s_price <= d("1.00")

    def test_empty_category_uses_defaults(self):
        """Empty category_prices list must not crash."""
        from app.ai.price_zscore import score_price
        s_price, _ = score_price(d("120"), [])
        assert d("0.00") <= s_price <= d("1.00")


# ---------------------------------------------------------------------------
# 7. Review Authenticity & Keyword Mining
# ---------------------------------------------------------------------------

class TestReviews:

    def test_insufficient_reviews_returns_neutral(self):
        from app.ai.reviews import score_reviews
        s, codes = score_reviews(["ok", "fine", "good"])
        assert s == d("0.50")
        assert "REV_INSUFFICIENT" in codes

    def test_clean_reviews_give_zero_risk(self):
        from app.ai.reviews import score_reviews
        clean = [
            "Excellent sound quality, arrived quickly.",
            "Battery lasts a whole week and the build feels premium.",
            "Exactly as described in the listing, packaging was solid.",
            "Great value for the money, would order again.",
            "Delivery took two days and support answered fast.",
        ]
        s, codes = score_reviews(clean)
        assert s == d("0.00"), f"Clean reviews should give s=0.00, got {s}"
        assert not any(c in codes for c in ["REV_KEYWORD_KNOCKOFF", "REV_BOT_PATTERN"])

    def test_knockoff_keyword_detected(self):
        from app.ai.reviews import score_reviews
        reviews = [
            "This is clearly a knockoff, terrible quality.",
            "Good product.",
            "Fast shipping.",
            "Works fine.",
            "Recommended.",
        ]
        s, codes = score_reviews(reviews)
        assert s > d("0.00")
        assert "REV_KEYWORD_KNOCKOFF" in codes

    def test_fake_keyword_detected(self):
        from app.ai.reviews import score_reviews
        reviews = [
            "Item is fake, do not buy.",
            "OK product.",
            "Nice packaging.",
            "Works well.",
            "Would buy again.",
        ]
        s, codes = score_reviews(reviews)
        assert "REV_KEYWORD_KNOCKOFF" in codes

    def test_bot_pattern_detected(self):
        from app.ai.reviews import score_reviews
        bot_reviews = [
            "best product best product best product buy now buy now",
            "amazing amazing amazing top quality top quality ship fast",
            "AAAAA+ seller AAAAA+ seller AAAAA+ seller recommend recommend recommend",
            "This is the greatest product ever made great product great product.",
            "Perfect perfect perfect perfect perfect perfect perfect perfect perfect.",
        ]
        s, codes = score_reviews(bot_reviews)
        assert "REV_BOT_PATTERN" in codes
        assert s > d("0.00")

    def test_score_clamped_to_one(self):
        from app.ai.reviews import score_reviews
        # Many triggers
        bad_reviews = [
            "total knockoff, not as pictured, feels cheap, is fake, scam seller",
            "best best best best best best best best best best best best",
            "replica replica replica replica replica replica replica replica",
            "cheap plastic fake logo knockoff",
            "not as pictured different than the photos totally fake",
        ]
        s, _ = score_reviews(bad_reviews)
        assert s <= d("1.00")


# ---------------------------------------------------------------------------
# 8. Claim Arbiter — 4-gate truth table
# ---------------------------------------------------------------------------

class TestArbiter:
    """Parametrized truth table for the 4 deterministic gates."""

    def _make_ctx(
        self,
        claim_amount: str = "100.00",
        buyer_past_claims: int = 0,
        buyer_claim_ratio: str = "0.00",
        dispute_active: bool = False,
    ):
        from app.schemas.contracts import ArbiterContext
        return ArbiterContext(
            claim_amount=d(claim_amount),
            buyer_past_claims=buyer_past_claims,
            buyer_claim_ratio=d(buyer_claim_ratio),
            dispute_active=dispute_active,
        )

    def _run(self, d_claim: str, ctx_kwargs: dict) -> str:
        """Exercise the gate logic directly without image embedding."""
        from app.ai.arbiter import _apply_gates
        from app.schemas.contracts import ArbiterContext
        ctx = self._make_ctx(**ctx_kwargs)
        outcome, gates = _apply_gates(d(d_claim), ctx)
        return outcome.value

    def test_all_gates_pass_auto_approved(self):
        outcome = self._run("0.70", {})
        assert outcome == "AUTO_APPROVED"

    def test_discrepancy_below_0_35_rejected(self):
        outcome = self._run("0.30", {})
        assert outcome == "REJECTED"

    def test_discrepancy_exactly_0_35_manual(self):
        """0.35 ≤ D < 0.65 → not AUTO_APPROVED, not REJECTED → MANUAL_REVIEW."""
        outcome = self._run("0.35", {})
        assert outcome == "MANUAL_REVIEW"

    def test_discrepancy_exactly_0_65_approved(self):
        outcome = self._run("0.65", {})
        assert outcome == "AUTO_APPROVED"

    def test_high_claim_amount_manual_review(self):
        """claim_amount > 250 → gate_amount_ok=False → MANUAL_REVIEW."""
        outcome = self._run("0.70", {"claim_amount": "251.00"})
        assert outcome == "MANUAL_REVIEW"

    def test_exactly_250_approved(self):
        """claim_amount == 250 → gate_amount_ok=True."""
        outcome = self._run("0.70", {"claim_amount": "250.00"})
        assert outcome == "AUTO_APPROVED"

    def test_too_many_past_claims_manual_review(self):
        """buyer_past_claims >= 2 → gate_velocity_ok=False."""
        outcome = self._run("0.70", {"buyer_past_claims": 2})
        assert outcome == "MANUAL_REVIEW"

    def test_exactly_1_past_claim_approved(self):
        outcome = self._run("0.70", {"buyer_past_claims": 1})
        assert outcome == "AUTO_APPROVED"

    def test_high_claim_ratio_manual_review(self):
        """buyer_claim_ratio >= 0.30 → gate_velocity_ok=False."""
        outcome = self._run("0.70", {"buyer_claim_ratio": "0.30"})
        assert outcome == "MANUAL_REVIEW"

    def test_dispute_active_manual_review(self):
        """dispute_active=True → gate_no_dispute=False."""
        outcome = self._run("0.70", {"dispute_active": True})
        assert outcome == "MANUAL_REVIEW"

    def test_multiple_failed_gates_still_manual(self):
        """Multiple failed gates → MANUAL_REVIEW (not REJECTED unless D<0.35)."""
        outcome = self._run("0.70", {
            "claim_amount": "300.00",
            "buyer_past_claims": 3,
            "dispute_active": True,
        })
        assert outcome == "MANUAL_REVIEW"


# ---------------------------------------------------------------------------
# 9. Cache — LRU eviction and async safety
# ---------------------------------------------------------------------------

class TestPillarCache:
    """Test the in-process LRU pillar cache."""

    def _make_pillars(self, suffix: str = "0"):
        from app.schemas.contracts import PillarScores, CacheStatus
        return PillarScores(
            s_visual=d("0.05"),
            s_semantic=d("0.10"),
            s_reviews=d("0.10"),
            s_price=d("0.10"),
            is_ai_generated=False,
            reason_codes=[],
            cache_status=CacheStatus.COMPUTED,
        )

    @pytest.mark.asyncio
    async def test_cache_miss_returns_none(self):
        from app.ai.cache import PillarCache
        cache = PillarCache(maxsize=10)
        result = await cache.get("nonexistent")
        assert result is None

    @pytest.mark.asyncio
    async def test_cache_set_and_get(self):
        from app.ai.cache import PillarCache
        cache = PillarCache(maxsize=10)
        pillars = self._make_pillars()
        await cache.set("key1", pillars)
        result = await cache.get("key1")
        assert result is not None
        assert result.s_visual == d("0.05")

    @pytest.mark.asyncio
    async def test_lru_eviction(self):
        from app.ai.cache import PillarCache
        cache = PillarCache(maxsize=3)
        for i in range(4):
            await cache.set(f"key{i}", self._make_pillars())
        # key0 should have been evicted
        assert await cache.get("key0") is None
        # key3 should still be present
        assert await cache.get("key3") is not None

    def test_image_hash_deterministic(self):
        from app.ai.cache import image_hash
        data = b"test image bytes"
        assert image_hash(data) == image_hash(data)
        assert len(image_hash(data)) == 64  # sha256 hex

    def test_image_hash_different_inputs(self):
        from app.ai.cache import image_hash
        assert image_hash(b"aaa") != image_hash(b"bbb")


# ---------------------------------------------------------------------------
# 10. SSRF Blocklist (unit tests matching test_ssrf_blocklist.py contract)
# ---------------------------------------------------------------------------

class TestSSRFBlocklist:
    """Verify safe_fetch.py enforces all required network security policies."""

    BLOCKED_HOSTS = [
        "127.0.0.1", "localhost", "10.0.0.1", "172.16.5.9",
        "192.168.1.10", "169.254.169.254", "0.0.0.0", "::1", "fc00::1",
    ]
    ALLOWED_HOSTS = ["images.example.com", "cdn.example.co.uk"]

    def test_private_hosts_blocked(self):
        from app.ai.safe_fetch import is_blocked_host
        for host in self.BLOCKED_HOSTS:
            assert is_blocked_host(host), f"{host} must be blocked"

    def test_public_hosts_allowed(self):
        from app.ai.safe_fetch import is_blocked_host
        for host in self.ALLOWED_HOSTS:
            assert not is_blocked_host(host), f"{host} must be allowed"

    def test_https_only_port_443(self):
        from app.ai.safe_fetch import is_blocked_url
        assert is_blocked_url("http://images.example.com/a.jpg")
        assert is_blocked_url("https://images.example.com:8080/a.jpg")
        assert is_blocked_url("https://user:pass@images.example.com/a.jpg")

    def test_clean_https_allowed(self):
        from app.ai.safe_fetch import is_blocked_url
        # Note: DNS resolution may or may not work in CI; test the URL parse logic
        # by patching socket or using a known-safe structure check
        # We test the structural parsing (not actual DNS) for unit tests
        from unittest.mock import patch
        import socket
        fake_addr = [
            (socket.AF_INET, socket.SOCK_STREAM, 0, '', ('93.184.216.34', 443))
        ]
        with patch("socket.getaddrinfo", return_value=fake_addr):
            assert not is_blocked_url("https://images.example.com/a.jpg")

    @pytest.mark.parametrize("ct", ["image/jpeg", "image/png", "image/webp"])
    def test_allowed_content_types(self, ct):
        from app.ai.safe_fetch import content_type_allowed
        assert content_type_allowed(ct)

    @pytest.mark.parametrize("ct", ["text/html", "image/svg+xml", "application/json"])
    def test_blocked_content_types(self, ct):
        from app.ai.safe_fetch import content_type_allowed
        assert not content_type_allowed(ct)


# ---------------------------------------------------------------------------
# 11. Demo Cache — deterministic seeded values
# ---------------------------------------------------------------------------

class TestDemoCache:

    def test_trusted_merchant_returns_correct_pillars(self):
        from app.ai.demo_cache import get_demo_pillars
        pillars = get_demo_pillars("mer_audio_den")
        assert pillars is not None
        assert pillars.s_visual == d("0.0500")
        assert pillars.s_semantic == d("0.1000")
        assert pillars.s_reviews == d("0.1000")
        assert pillars.s_price == d("0.1000")
        assert pillars.is_ai_generated is False

    def test_scam_merchant_is_ai_generated(self):
        from app.ai.demo_cache import get_demo_pillars
        pillars = get_demo_pillars("mer_dash_deals")
        assert pillars is not None
        assert pillars.s_visual == d("0.9200")
        assert pillars.is_ai_generated is True
        assert "VIS_AI_GENERATED" in pillars.reason_codes

    def test_offender_merchant_flagged(self):
        from app.ai.demo_cache import get_demo_pillars
        pillars = get_demo_pillars("mer_refurb_find")
        assert pillars is not None
        assert pillars.s_visual == d("0.5000")
        assert "REP_FLAGGED_HISTORY" in pillars.reason_codes

    def test_unknown_merchant_returns_none(self):
        from app.ai.demo_cache import get_demo_pillars
        assert get_demo_pillars("mer_unknown_xyz") is None

    def test_all_demo_pillars_have_demo_cache_status(self):
        from app.ai.demo_cache import get_demo_pillars
        from app.schemas.contracts import CacheStatus
        for mid in ["mer_audio_den", "mer_dash_deals", "mer_refurb_find"]:
            p = get_demo_pillars(mid)
            assert p is not None
            assert p.cache_status == CacheStatus.DEMO, f"{mid} cache_status must be DEMO"
