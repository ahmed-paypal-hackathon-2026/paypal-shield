"""Response models mirroring the frozen shared/openapi.json (Person 1).

CONTRACT NOTE (do not delete): Person 4 owns schemas/contracts.py. The frozen
OpenAPI contract already contains the schemas below, but the Pydantic models
were not added to contracts.py. Until Person 4 promotes them, this mirror
module defines them with identical names/shapes so the exported OpenAPI
matches shared/openapi.json exactly (tests/integration/test_openapi_frozen.py).
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field

from app.schemas.contracts import RiskAssessment, ProtectionQuote


class QuoteResponse(BaseModel):
    assessment: RiskAssessment
    quote: ProtectionQuote | None


class TransactionRow(BaseModel):
    internal_order_id: str
    merchant_id: str
    item_price: Decimal
    protection_fee: Decimal
    r_final: Decimal
    state: str
    created_at: datetime


class MerchantTrustRow(BaseModel):
    merchant_id: str
    total_orders: int = Field(ge=0, json_schema_extra={"minimum": 0})
    successful_orders: int = Field(ge=0, json_schema_extra={"minimum": 0})
    verified_fraud_claims: int = Field(ge=0, json_schema_extra={"minimum": 0})
    trust_score: Decimal
    m_penalty: Decimal


class LedgerSummary(BaseModel):
    total_volume: Decimal
    protection_fees_collected: Decimal
    refunds_issued: Decimal
    net_loss_ratio: Decimal


class HealthResponse(BaseModel):
    status: str
    paypal_sandbox_connected: bool
    demo_mode: bool


__all__ = [
    "QuoteResponse",
    "TransactionRow",
    "MerchantTrustRow",
    "LedgerSummary",
    "HealthResponse",
]
