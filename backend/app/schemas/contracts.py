"""Canonical Pydantic contracts - owned by Person 4 (DevOps/QA Lead).

Backend routers and services must import these read-only. Any change requires
a contract version bump plus sign-off from all owners. Money fields are
decimal.Decimal and serialize as JSON strings (Pydantic v2 JSON mode).
Contract version: 1.0.0 (frozen, spec section 5.1).
"""

from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import List, Optional, Protocol

from pydantic import BaseModel, Field

CONTRACT_VERSION = "1.0.0"


class CacheStatus(str, Enum):
    HIT = "HIT"
    COMPUTED = "COMPUTED"
    MISS_DEFAULT = "MISS_DEFAULT"
    DEMO = "DEMO"


class GateDecision(str, Enum):
    COVERAGE_AVAILABLE = "COVERAGE_AVAILABLE"
    BLOCKED_HIGH_RISK = "BLOCKED_HIGH_RISK"
    MANUAL_REVIEW = "MANUAL_REVIEW"
    UNSUPPORTED_CURRENCY = "UNSUPPORTED_CURRENCY"


class ReasonCode(str, Enum):
    """Canonical vocabulary for pillar reason codes (spec section 6.1).

    P2 must emit only these values. P3 maps them to human-readable labels
    via frontend/src/components/RiskBadge/reasons.ts.
    """

    VIS_AI_GENERATED = "VIS_AI_GENERATED"
    VIS_FALLBACK_DEFAULT = "VIS_FALLBACK_DEFAULT"
    SEM_LOW_ALIGNMENT = "SEM_LOW_ALIGNMENT"
    REV_KEYWORD_KNOCKOFF = "REV_KEYWORD_KNOCKOFF"
    REV_BOT_PATTERN = "REV_BOT_PATTERN"
    REV_INSUFFICIENT = "REV_INSUFFICIENT"
    PRICE_EXTREME_UNDER = "PRICE_EXTREME_UNDER"
    REP_FLAGGED_HISTORY = "REP_FLAGGED_HISTORY"
    REP_VERIFIED_HISTORY = "REP_VERIFIED_HISTORY"


class PillarScores(BaseModel):
    s_visual: Decimal = Field(..., ge=0, le=1)
    s_semantic: Decimal = Field(..., ge=0, le=1)
    s_reviews: Decimal = Field(..., ge=0, le=1)
    s_price: Decimal = Field(..., ge=0, le=1)
    is_ai_generated: bool
    reason_codes: List[str] = []
    model_versions: dict[str, str] = Field(default_factory=dict)
    cache_status: CacheStatus


class MerchantReputation(BaseModel):
    merchant_id: str
    total_orders: int = Field(..., ge=0)
    successful_orders: int = Field(..., ge=0)
    verified_fraud_claims: int = Field(..., ge=0)


class RiskAssessment(BaseModel):
    item_hash: str
    merchant_id: str
    pillars: PillarScores
    trust_score: Decimal
    s_merchant: Decimal
    r_base: Decimal
    m_penalty: Decimal
    r_final: Decimal  # percent, two decimals
    gate: GateDecision


class ProtectionQuote(BaseModel):
    quote_id: str
    merchant_id: str
    item_hash: str
    item_price: Decimal
    currency_code: str
    protection_fee: Decimal
    expires_at: datetime
    hmac_signature: str


class QuoteRequest(BaseModel):
    merchant_id: str
    item_hash: str
    item_title: str
    item_price: Decimal
    currency_code: str


class CreateOrderRequest(BaseModel):
    quote_id: str
    opt_in: bool
    buyer_ref: str


class CreateOrderResponse(BaseModel):
    order_id: str  # PayPal order id
    internal_order_id: str
    status: str
    total: Decimal
    currency_code: str


class CaptureResponse(BaseModel):
    order_id: str
    paypal_capture_id: str
    status: str


class ClaimState(str, Enum):
    SUBMITTED = "SUBMITTED"
    EVIDENCE_CHECKED = "EVIDENCE_CHECKED"
    AUTO_APPROVED = "AUTO_APPROVED"
    MANUAL_REVIEW = "MANUAL_REVIEW"
    REJECTED = "REJECTED"
    REFUNDED = "REFUNDED"


class ArbiterDecision(BaseModel):
    claim_id: str
    discrepancy_score: Decimal = Field(..., ge=0, le=1)
    gate_amount_ok: bool
    gate_velocity_ok: bool
    gate_no_dispute: bool
    gate_discrepancy_ok: bool
    outcome: ClaimState  # AUTO_APPROVED | MANUAL_REVIEW | REJECTED
    reason_codes: List[str]


class ClaimRecord(BaseModel):
    claim_id: str
    order_id: str
    state: ClaimState
    claim_amount: Decimal
    currency_code: str
    decision: Optional[ArbiterDecision] = None
    refund_id: Optional[str] = None
    updated_at: datetime


class ArbiterContext(BaseModel):
    """Runtime context supplied to RiskEngine.arbitrate() (spec section 6.5).

    Owned by Person 4; consumed by Person 2's arbiter implementation.
    All monetary fields use Decimal and serialize as JSON strings.
    """

    claim_amount: Decimal
    currency_code: str = "USD"
    buyer_past_claims: int = 0
    buyer_claim_ratio: Decimal = Decimal("0.00")
    dispute_active: bool = False
    listing_category: Optional[str] = None
    listing_price: Optional[Decimal] = None


class RiskEngine(Protocol):
    async def precompute(
        self,
        listing_id: str,
        image_bytes: bytes,
        title: str,
        description: str,
        price: Decimal,
        category: str,
        reviews: list[str],
    ) -> PillarScores: ...

    def assess(
        self, req: QuoteRequest, pillars: PillarScores, rep: MerchantReputation
    ) -> RiskAssessment: ...

    def price_fee(
        self, a: RiskAssessment, item_price: Decimal, currency: str
    ) -> Decimal: ...

    async def arbitrate(
        self,
        claim: ClaimRecord,
        listing_image: bytes,
        received_image: bytes,
        ctx: ArbiterContext,
    ) -> ArbiterDecision: ...
