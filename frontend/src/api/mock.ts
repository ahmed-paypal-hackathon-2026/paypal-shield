import type {
  ClaimRecord,
  ClaimState,
  LedgerSummary,
  MerchantTrustRow,
  QuoteResponse,
  RiskAssessment,
  TransactionRow,
} from "../types/contracts";
import type {
  CaptureResponse,
  CreateOrderRequest,
  CreateOrderResponse,
  HealthStatus,
  QuoteRequest,
} from "../types/requests";
import type { ShieldApi } from "./client";
import type { StreamEnvelope, StreamHandler, Unsubscribe } from "./stream";

const now = () => new Date().toISOString();

/** Parses a decimal Money string into BigInt cents. Integer arithmetic only. */
export function toCents(value: string): bigint {
  const [whole, frac = ""] = value.replace(/^-/, "").split(".");
  return BigInt(whole) * 100n + BigInt(frac.padEnd(2, "0").slice(0, 2));
}

export function fromCents(cents: bigint): string {
  return `${cents / 100n}.${(cents % 100n).toString().padStart(2, "0")}`;
}

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

type ScenarioId = "trusted" | "scam" | "offender";

const TRUSTED = "mer_audio_den";
const SCAM = "mer_dash_deals";
const OFFENDER = "mer_refurb_find";

interface ScenarioSeed {
  merchant: MerchantTrustRow;
  itemPrice: string;
  itemHash: string;
  assessment: Pick<
    RiskAssessment,
    "pillars" | "trust_score" | "s_merchant" | "r_base" | "r_final" | "gate" | "m_penalty"
  >;
  fee: string | null;
}

const seeds: Record<ScenarioId, ScenarioSeed> = {
  trusted: {
    merchant: { merchant_id: TRUSTED, total_orders: 200, successful_orders: 190, verified_fraud_claims: 0, trust_score: "0.9381", m_penalty: "1.00" },
    itemPrice: "120.00",
    itemHash: "b3a9e55dde37b80d97fa8713a01dda91d3c1c7f2e33bd0cf8a35f4f4e3a11c05",
    assessment: {
      pillars: {
        s_visual: "0.0500", s_semantic: "0.1000", s_reviews: "0.1000", s_price: "0.1000",
        is_ai_generated: false, reason_codes: [], cache_status: "HIT",
      },
      trust_score: "0.9381", s_merchant: "0.0619", r_base: "0.0780", r_final: "7.80", gate: "COVERAGE_AVAILABLE", m_penalty: "1.00",
    },
    fee: "0.47",
  },
  scam: {
    merchant: { merchant_id: SCAM, total_orders: 2, successful_orders: 2, verified_fraud_claims: 0, trust_score: "0.7500", m_penalty: "1.00" },
    itemPrice: "80.00",
    itemHash: "9d40b7a2c02df11bff6f398ba9dfde04c8f2fba666561e4b7a4d9b4c4f00ad21",
    assessment: {
      pillars: {
        s_visual: "0.9200", s_semantic: "0.8500", s_reviews: "0.7000", s_price: "0.9000",
        is_ai_generated: true, reason_codes: ["VIS_AI_GENERATED", "SEM_LOW_ALIGNMENT", "PRICE_EXTREME_UNDER"], cache_status: "DEMO",
      },
      trust_score: "0.7500", s_merchant: "0.2500", r_base: "0.7025", r_final: "70.25", gate: "COVERAGE_AVAILABLE", m_penalty: "1.00",
    },
    fee: "2.81",
  },
  offender: {
    merchant: { merchant_id: OFFENDER, total_orders: 40, successful_orders: 30, verified_fraud_claims: 2, trust_score: "0.7400", m_penalty: "2.00" },
    itemPrice: "60.00",
    itemHash: "4c8d2ca1d5d63a1eb9b60a15b4a1f8bff38dcdbf7a8c3d10b8dcfc8d3a417bae",
    assessment: {
      pillars: {
        s_visual: "0.5000", s_semantic: "0.4000", s_reviews: "0.6000", s_price: "0.3000",
        is_ai_generated: false, reason_codes: ["REP_FLAGGED_HISTORY"], cache_status: "DEMO",
      },
      trust_score: "0.7400", s_merchant: "0.2600", r_base: "0.4050", r_final: "81.00", gate: "BLOCKED_HIGH_RISK", m_penalty: "2.00",
    },
    fee: null,
  },
};

const merchantToScenario: Record<string, ScenarioId> = {
  [TRUSTED]: "trusted",
  [SCAM]: "scam",
  [OFFENDER]: "offender",
};

let seq = 0;
const uid = (prefix: string) => `${prefix}_${(++seq).toString(16).padStart(4, "0")}`;

interface PendingOrder {
  scenario: ScenarioId;
  optIn: boolean;
  fee: string;
}

export class MockApi implements ShieldApi {
  readonly mode = "mock" as const;

  private _claims: ClaimRecord[] = [
    {
      claim_id: uid("clm"), order_id: "ord_01JDEM0", state: "MANUAL_REVIEW",
      claim_amount: "60.00", currency_code: "USD", updated_at: now(),
      reason_codes: ["ARB_VELOCITY_LIMIT"],
    },
  ];

  private _transactions: TransactionRow[] = [
    { internal_order_id: "ord_01JDEM0", merchant_id: OFFENDER, item_price: "60.00", protection_fee: "0.00", r_final: "81.00", state: "CAPTURED", created_at: now() },
  ];

  private _merchantsState: MerchantTrustRow[] = Object.values(seeds).map((s) => ({ ...s.merchant }));

  private _summary: LedgerSummary = {
    total_volume: "60.00", protection_fees_collected: "0.00",
    refunds_issued: "0.00", net_loss_ratio: "0.0000",
  };

  private _pendingOrders = new Map<string, PendingOrder>();
  private _quoteScenarios = new Map<string, ScenarioId>();
  private _claimTimers: number[] = [];
  private _handlers = new Set<StreamHandler>();

  health(): Promise<HealthStatus> {
    return Promise.resolve({ status: "ok", paypal_sandbox_connected: false, demo_mode: true });
  }

  quote(req: QuoteRequest): Promise<QuoteResponse> {
    const scenario = merchantToScenario[req.merchant_id] ?? "trusted";
    const seed = seeds[scenario];
    const assessment: RiskAssessment = {
      item_hash: seed.itemHash, merchant_id: req.merchant_id, ...seed.assessment,
    };
    let quote: QuoteResponse["quote"] = null;
    if (seed.fee) {
      quote = {
        quote_id: uid("quote"), merchant_id: req.merchant_id, item_hash: seed.itemHash,
        item_price: seed.itemPrice, currency_code: req.currency_code,
        protection_fee: seed.fee, expires_at: new Date(Date.now() + 15 * 60_000).toISOString(),
        hmac_signature: "demo-signature",
      };
      this._quoteScenarios.set(quote.quote_id, scenario);
    }
    return sleep(250).then(() => ({ assessment, quote }));
  }

  createOrder(req: CreateOrderRequest): Promise<CreateOrderResponse> {
    const scenario = this._quoteScenarios.get(req.quote_id) ?? "trusted";
    const seed = seeds[scenario];
    const fee = req.opt_in && seed.fee ? seed.fee : "0.00";
    const resp: CreateOrderResponse = {
      order_id: uid("PCAP").toUpperCase(), internal_order_id: uid("ord_"),
      status: "CREATED", total: fromCents(toCents(seed.itemPrice) + toCents(fee)),
      currency_code: "USD",
    };
    this._pendingOrders.set(resp.internal_order_id, { scenario, optIn: req.opt_in, fee });
    return sleep(300).then(() => resp);
  }

  capture(orderId: string): Promise<CaptureResponse> {
    const pending = this._pendingOrders.get(orderId);
    const merchantId = pending ? seeds[pending.scenario].merchant.merchant_id : TRUSTED;
    const itemPrice = pending ? seeds[pending.scenario].itemPrice : "0.00";
    const fee = pending?.fee ?? "0.00";
    const row: TransactionRow = {
      internal_order_id: orderId, merchant_id: merchantId, item_price: itemPrice,
      protection_fee: fee, r_final: pending ? seeds[pending.scenario].assessment.r_final : "0.00",
      state: "CAPTURED", created_at: now(),
    };
    this._transactions = [row, ...this._transactions];
    this._summary = {
      ...this._summary,
      total_volume: fromCents(toCents(this._summary.total_volume) + toCents(itemPrice)),
      protection_fees_collected: fromCents(toCents(this._summary.protection_fees_collected) + toCents(fee)),
    };
    this._merchantsState = this._merchantsState.map((m) =>
      m.merchant_id === merchantId
        ? { ...m, successful_orders: m.successful_orders + 1, total_orders: m.total_orders + 1 }
        : m,
    );
    this.emit({ topic: "transaction", action: "add", record: row });
    this.emit({ topic: "merchant", action: "update", record: this._merchantsState.find((m) => m.merchant_id === merchantId)! });
    return sleep(350).then(() => ({
      order_id: orderId, paypal_capture_id: uid("8YF"), status: "COMPLETED",
    }));
  }

  submitClaim(orderId: string, _photo: File): Promise<ClaimRecord> {
    const txn = this._transactions.find((t) => t.internal_order_id === orderId);
    const claim: ClaimRecord = {
      claim_id: uid("clm"), order_id: orderId, state: "SUBMITTED",
      claim_amount: txn?.item_price ?? "0.00", currency_code: "USD", updated_at: now(),
    };
    this._claims = [claim, ...this._claims];
    this.emit({ topic: "claim", action: "add", record: claim });
    this.walkPipeline(claim);
    return sleep(200).then(() => claim);
  }

  resolveClaim(claimId: string, decision: "REFUND" | "REJECT", _reasonCode: string): Promise<ClaimRecord> {
    const claim = this._claims.find((c) => c.claim_id === claimId);
    if (!claim) throw new Error("unknown claim");
    return this.advanceClaim(claim.claim_id, decision === "REFUND" ? "REFUNDED" : "REJECTED", decision === "REFUND" ? uid("8YF") : undefined).then(
      () => this._claims.find((c) => c.claim_id === claimId)!,
    );
  }

  transactions(): Promise<TransactionRow[]> {
    return sleep(120).then(() => [...this._transactions].reverse());
  }

  merchants(): Promise<MerchantTrustRow[]> {
    return sleep(120).then(() => [...this._merchantsState]);
  }

  summary(): Promise<LedgerSummary> {
    return sleep(100).then(() => ({ ...this._summary }));
  }

  subscribe(handler: StreamHandler): Unsubscribe {
    this._handlers.add(handler);
    return () => {
      this._handlers.delete(handler);
    };
  }

  stop(): void {
    for (const id of this._claimTimers) window.clearTimeout(id);
    this._claimTimers = [];
    this._handlers.clear();
  }

  private walkPipeline(claim: ClaimRecord) {
    const scenario = this._pendingOrders.get(claim.order_id)?.scenario ?? "trusted";
    const steps: Array<{ state: ClaimState; delay: number }> =
      scenario === "scam"
        ? [
            { state: "EVIDENCE_CHECKED", delay: 900 },
            { state: "AUTO_APPROVED", delay: 1300 },
            { state: "REFUNDED", delay: 1400 },
          ]
        : scenario === "offender"
          ? [
              { state: "EVIDENCE_CHECKED", delay: 900 },
              { state: "MANUAL_REVIEW", delay: 1300 },
            ]
          : [
              { state: "EVIDENCE_CHECKED", delay: 900 },
              { state: "REJECTED", delay: 1300 },
            ];
    let elapsed = 700;
    for (const step of steps) {
      const id = window.setTimeout(() => {
        void this.advanceClaim(claim.claim_id, step.state, step.state === "REFUNDED" ? uid("8YF") : undefined);
      }, elapsed);
      elapsed += step.delay;
      this._claimTimers.push(id);
    }
  }

  private async advanceClaim(claimId: string, state: ClaimState, refundId?: string) {
    const claim = this._claims.find((c) => c.claim_id === claimId);
    if (!claim) return null;
    const updated: ClaimRecord = {
      ...claim, state, updated_at: now(),
      ...(refundId ? { refund_id: refundId } : {}),
    };
    this._claims = this._claims.map((c) => (c.claim_id === claimId ? updated : c));
    this.emit({ topic: "claim", action: "update", record: updated });
    if (state === "REFUNDED") {
      const txn = this._transactions.find((t) => t.internal_order_id === claim.order_id);
      this._summary = {
        ...this._summary,
        refunds_issued: fromCents(toCents(this._summary.refunds_issued) + toCents(claim.claim_amount)),
        net_loss_ratio: "0.0500",
      };
      if (txn) {
        this._merchantsState = this._merchantsState.map((m) =>
          m.merchant_id === txn.merchant_id
            ? { ...m, verified_fraud_claims: m.verified_fraud_claims + 1 }
            : m,
        );
        this.emit({
          topic: "merchant", action: "update",
          record: this._merchantsState.find((m) => m.merchant_id === txn.merchant_id)!,
        });
      }
    }
    return updated;
  }

  private emit(envelope: StreamEnvelope) {
    for (const h of this._handlers) h(envelope);
  }
}

export const sharedMock = new MockApi();
