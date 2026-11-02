// Canonical TypeScript contract - owned by Person 4 (DevOps/QA Lead).
// Frontend must import these types read-only. Any change requires a contract
// version bump plus sign-off from all owners. Money is always a decimal
// string: the UI never performs floating-point money math.
// Contract version: 1.0.0 (frozen, spec section 5.3).

// Money is always a decimal string. The frontend never does float money math.
export type Money = string;

export type GateDecision =
  | "COVERAGE_AVAILABLE" | "BLOCKED_HIGH_RISK"
  | "MANUAL_REVIEW" | "UNSUPPORTED_CURRENCY";

export type ClaimState =
  | "SUBMITTED" | "EVIDENCE_CHECKED" | "AUTO_APPROVED"
  | "MANUAL_REVIEW" | "REJECTED" | "REFUNDED";

/** Canonical vocabulary for pillar reason codes (spec section 6.1).
 *  P2 emits only these values. Map to human labels in RiskBadge/reasons.ts.
 */
export type ReasonCode =
  | "VIS_AI_GENERATED"
  | "VIS_FALLBACK_DEFAULT"
  | "SEM_LOW_ALIGNMENT"
  | "REV_KEYWORD_KNOCKOFF"
  | "REV_BOT_PATTERN"
  | "REV_INSUFFICIENT"
  | "PRICE_EXTREME_UNDER"
  | "REP_FLAGGED_HISTORY"
  | "REP_VERIFIED_HISTORY";

export interface PillarScores {
  s_visual: string; s_semantic: string; s_reviews: string; s_price: string;
  is_ai_generated: boolean; reason_codes: string[];
  cache_status: "HIT" | "COMPUTED" | "MISS_DEFAULT" | "DEMO";
  /** AI model versions used per pillar, keyed by pillar name. */
  model_versions?: Record<string, string>;
}

export interface RiskAssessment {
  item_hash: string; merchant_id: string; pillars: PillarScores;
  trust_score: string; s_merchant: string; r_base: string;
  m_penalty: string; r_final: string; gate: GateDecision;
}

export interface ProtectionQuote {
  quote_id: string; merchant_id: string; item_hash: string;
  item_price: Money; currency_code: string; protection_fee: Money;
  expires_at: string; hmac_signature: string;
}

export interface QuoteResponse { assessment: RiskAssessment; quote: ProtectionQuote | null; }

export interface ClaimRecord {
  claim_id: string; order_id: string; state: ClaimState;
  claim_amount: Money; currency_code: string; updated_at: string;
  refund_id?: string; reason_codes?: string[];
}

export interface LedgerSummary {
  total_volume: Money; protection_fees_collected: Money;
  refunds_issued: Money; net_loss_ratio: string;
}

export interface TransactionRow {
  internal_order_id: string; merchant_id: string; item_price: Money;
  protection_fee: Money; r_final: string; state: string; created_at: string;
}

export interface MerchantTrustRow {
  merchant_id: string; total_orders: number; successful_orders: number;
  verified_fraud_claims: number; trust_score: string; m_penalty: string;
}

/** Runtime context supplied to the arbiter pipeline (spec section 6.5).
 *  Mirrors ArbiterContext in backend/app/schemas/contracts.py.
 *  All Money fields are decimal strings — no float arithmetic.
 */
export interface ArbiterContext {
  claim_amount: Money;
  currency_code: string;
  buyer_past_claims: number;
  buyer_claim_ratio: string;
  dispute_active: boolean;
  listing_category?: string;
  listing_price?: Money;
}
