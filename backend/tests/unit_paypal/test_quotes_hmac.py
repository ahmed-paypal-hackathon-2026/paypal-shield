"""Signed quote verification (Person 1, REQ-BE-06).

Any single-byte change to ANY signed field must fail verification;
comparison is constant-time; keys support rotation; expiry enforced.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.config import Settings
from app.schemas.contracts import GateDecision, QuoteRequest, RiskAssessment
from app.services import quotes as Q
from app.services.engine_provider import fallback_engine
from app.services.quotes import (
    QuoteError,
    QuoteExpiredError,
    QuoteSignatureError,
    canonical_quote_string,
    sign_canonical,
)

# Anchor relative to real now so fixed dates can never flake the expiry logic.
START = datetime.now(timezone.utc)


def _settings(**kw) -> Settings:
    base = dict(quotes_hmac_key="k-current-0123456789abcdef", _env_file=None)
    base.update(kw)
    return Settings(**base)


@pytest.fixture
def sig():
    # Signature as it would be produced server-side
    canonical = canonical_quote_string(
        quote_id="q_abc",
        merchant_id="mer_demo",
        item_hash="a" * 64,
        item_price=Decimal("120.00"),
        currency_code="USD",
        protection_fee=Decimal("0.47"),
        expires_at=START + timedelta(seconds=600),
    )
    return sign_canonical(canonical, ["k-current-0123456789abcdef"])


def _quote(sig: str, **overrides):
    fields = dict(
        quote_id="q_abc",
        merchant_id="mer_demo",
        item_hash="a" * 64,
        item_price=Decimal("120.00"),
        currency_code="USD",
        protection_fee=Decimal("0.47"),
        expires_at=START + timedelta(seconds=600),
        hmac_signature=sig,
    )
    fields.update(overrides)
    from app.schemas.contracts import ProtectionQuote

    return ProtectionQuote(**fields)


class TestHmacVerification:
    def test_valid_quote_verify_ok(self, sig):
        Q.verify_quote(_quote(sig), _settings())  # no raise

    @pytest.mark.parametrize(
        "field, bad",
        [
            ("quote_id", "q_zzz"),
            ("merchant_id", "mer_evil"),
            ("item_hash", "b" * 64),
            ("item_price", Decimal("121.00")),
            ("currency_code", "EUR"),
            ("protection_fee", Decimal("0.48")),
            ("expires_at", START + timedelta(seconds=601)),
            ("hmac_signature", "f" * 64),
        ],
        ids=[
            "quote_id", "merchant_id", "item_hash", "item_price",
            "currency_code", "protection_fee", "expires_at", "signature",
        ],
    )
    def test_any_single_field_tamper_rejected(self, sig, field, bad):
        with pytest.raises((QuoteSignatureError, QuoteExpiredError)):
            Q.verify_quote(_quote(sig, **{field: bad}), _settings(
                # use same lifetime so only tampering explains failure
                quotes_ttl_seconds=6000,
            ))
        # (expiry path separately checked below; tampering expiry must fail sig)

    def test_constant_time_helper_rejects_wrong_key(self, sig):
        canonical = canonical_quote_string(
            quote_id="q_abc", merchant_id="mer_demo", item_hash="a" * 64,
            item_price=Decimal("120.00"), currency_code="USD",
            protection_fee=Decimal("0.47"),
            expires_at=START + timedelta(seconds=600),
        )
        assert Q.verify_signature_constant_time("k-current-0123456789abcdef", canonical, sig)
        assert not Q.verify_signature_constant_time("k-other", canonical, sig)

    def test_rotation_previous_key_accepted(self, sig):
        # Quote signed during the previous key's lifetime stays valid while
        # the previous key is still configured.
        s = _settings(quotes_hmac_key="k-new", quotes_hmac_key_previous="k-current-0123456789abcdef")
        Q.verify_quote(_quote(sig), s)
        # After rotation drops the old key, old quotes are rejected.
        with pytest.raises(QuoteSignatureError):
            Q.verify_quote(_quote(sig), _settings(quotes_hmac_key="k-new"))


class TestExpiry:
    def test_expired_quote_rejected(self, sig):
        past = START - timedelta(seconds=1)
        q = _quote(sig, expires_at=START)
        q = q.model_copy(update={"expires_at": past})
        # expired quotes ALSO carry a differring signed expiry -> signature invalid first
        with pytest.raises(QuoteError):
            Q.verify_quote(q, _settings())
        # but expiry-only failure surfaces when canonical still matches:
        canonical = canonical_quote_string(
            quote_id="q_abc", merchant_id="mer_demo", item_hash="a" * 64,
            item_price=Decimal("120.00"), currency_code="USD",
            protection_fee=Decimal("0.47"), expires_at=past,
        )
        old_sig = sign_canonical(canonical, ["k-current-0123456789abcdef"])
        q2 = _quote(old_sig, expires_at=past)
        with pytest.raises(QuoteExpiredError):
            Q.verify_quote(q2, _settings())

    def test_expiry_check_disabled(self, sig):
        Q.verify_quote(_quote(sig), _settings(), check_expiry=False)


class TestCanonicalisation:
    def test_decimal_rendering_never_exponent(self):
        assert Q._fmt_decimal(Decimal("1E+2")) == "100"
        assert Q._fmt_decimal(Decimal("0.470")) == "0.470"

    def test_separator_collision_rejected(self):
        with pytest.raises(QuoteError):
            canonical_quote_string(
                quote_id="q|bad", merchant_id="m", item_hash="a" * 64,
                item_price=Decimal("1.00"), currency_code="USD",
                protection_fee=Decimal("0.25"), expires_at=START,
            )

    def test_naive_datetime_treated_utc(self):
        assert Q._fmt_datetime(START.replace(tzinfo=None)).endswith("+00:00")


class TestBuildQuote:
    def _assessment(self, gate=GateDecision.COVERAGE_AVAILABLE, r_final=Decimal("45.00")):
        return RiskAssessment.model_construct(
            item_hash="a" * 64, merchant_id="mer_demo",
            trust_score=Decimal("0.70"), s_merchant=Decimal("0.3000"),
            r_base=Decimal("0.4500"), m_penalty=Decimal("1.00"),
            r_final=r_final, gate=gate,
        )

    def _req(self):
        return QuoteRequest(
            merchant_id="mer_demo", item_hash="a" * 64,
            item_title="Widget", item_price=Decimal("120.00"), currency_code="USD",
        )

    def test_build_signs_and_persists_shape(self):
        s = _settings()
        quote = Q.build_quote(self._assessment(), self._req(), s, ttl_seconds=600)
        assert quote is not None
        assert quote.hmac_signature and len(quote.hmac_signature) == 64
        Q.verify_quote(quote, s)  # signed round trip verifies

    def test_blocked_gate_returns_no_quote(self):
        assert Q.build_quote(self._assessment(gate=GateDecision.BLOCKED_HIGH_RISK), self._req(), _settings()) is None

    def test_zero_fee_declines_quote(self, monkeypatch):
        # Engine says available but computes fee 0 -> no quote is issued.
        import app.services.engine_provider as ep

        monkeypatch.setattr(
            ep, "get_risk_engine",
            lambda: type("E", (), {"price_fee": staticmethod(lambda a, p, c: Decimal("0.00"))})(),
        )
        assert Q.build_quote(self._assessment(), self._req(), _settings()) is None

    def test_engine_failure_falls_back_to_stub(self, monkeypatch):
        def boom(a, p, c):
            raise RuntimeError("engine exploded")

        import app.services.engine_provider as ep

        monkeypatch.setattr(ep, "get_risk_engine", lambda: type("E", (), {"price_fee": staticmethod(boom)})())
        quote = Q.build_quote(self._assessment(), self._req(), _settings())
        assert quote is not None  # REQ-NF-03: never fail the quote path
        Q.verify_quote(quote, _settings())


class TestStubEngineMath:
    """Worked examples from spec section 6.3 (verified calculations)."""

    def test_trusted_scenario_fee_047(self):
        from app.schemas.contracts import CacheStatus, PillarScores

        stub = fallback_engine()
        pillars = PillarScores(
            s_visual=Decimal("0.05"), s_semantic=Decimal("0.10"),
            s_reviews=Decimal("0.10"), s_price=Decimal("0.10"),
            is_ai_generated=False, cache_status=CacheStatus.HIT,
        )
        from app.schemas.contracts import MerchantReputation

        rep = MerchantReputation(merchant_id="m", total_orders=200, successful_orders=190, verified_fraud_claims=0)
        req = QuoteRequest(merchant_id="m", item_hash="x", item_title="t",
                           item_price=Decimal("120.00"), currency_code="USD")
        a = stub.assess(req, pillars, rep)
        assert a.trust_score == Decimal("0.9381")
        assert a.r_final == Decimal("7.80")
        assert a.gate == GateDecision.COVERAGE_AVAILABLE
        assert stub.price_fee(a, Decimal("120.00"), "USD") == Decimal("0.47")

    def test_repeat_offender_blocked(self):
        from app.schemas.contracts import CacheStatus, MerchantReputation, PillarScores

        stub = fallback_engine()
        pillars = PillarScores(
            s_visual=Decimal("0.50"), s_semantic=Decimal("0.40"),
            s_reviews=Decimal("0.60"), s_price=Decimal("0.30"),
            is_ai_generated=False, cache_status=CacheStatus.HIT,
        )
        rep = MerchantReputation(merchant_id="m", total_orders=40, successful_orders=30,
                                 verified_fraud_claims=2)
        req = QuoteRequest(merchant_id="m", item_hash="x", item_title="t",
                           item_price=Decimal("60.00"), currency_code="USD")
        a = stub.assess(req, pillars, rep)
        assert a.r_final == Decimal("81.00")
        assert a.gate == GateDecision.BLOCKED_HIGH_RISK
        assert stub.price_fee(a, Decimal("60.00"), "USD") == Decimal("0.00")

    def test_unsupported_currency(self):
        from app.schemas.contracts import CacheStatus, MerchantReputation, PillarScores

        stub = fallback_engine()
        pillars = PillarScores(
            s_visual=Decimal("0.1"), s_semantic=Decimal("0.1"),
            s_reviews=Decimal("0.1"), s_price=Decimal("0.1"),
            is_ai_generated=False, cache_status=CacheStatus.HIT,
        )
        rep = MerchantReputation(merchant_id="m", total_orders=10, successful_orders=10, verified_fraud_claims=0)
        req = QuoteRequest(merchant_id="m", item_hash="x", item_title="t",
                           item_price=Decimal("100.00"), currency_code="AUD")
        a = stub.assess(req, pillars, rep)
        assert a.gate == GateDecision.UNSUPPORTED_CURRENCY
