import { create } from "zustand";
import type {
  ClaimRecord,
  LedgerSummary,
  MerchantTrustRow,
  QuoteResponse,
  TransactionRow,
} from "../types/contracts";
import type {
  CaptureResponse,
  CreateOrderRequest,
  CreateOrderResponse,
  HealthStatus,
} from "../types/requests";
import type { ShieldApi } from "../api/client";
import { ApiError, createApi } from "../api/client";
import type { StreamEnvelope } from "../api/stream";
import type { ScenarioId } from "../data/scenarios";
import { scenarioById } from "../data/scenarios";

export type CheckoutPhase =
  | "idle"
  | "creating_order"
  | "awaiting_approval"
  | "capturing"
  | "captured"
  | "failed";

export type ClaimStatus = "idle" | "uploading" | "submitted" | "error";

interface ShieldState {
  api: ShieldApi | null;
  mode: "connecting" | "live" | "mock";
  health: HealthStatus | null;
  scenario: ScenarioId | null;
  assessment: QuoteResponse["assessment"] | null;
  quote: QuoteResponse["quote"] | null;
  quoteStatus: "idle" | "loading" | "ready" | "error";
  quoteError: string;
  optIn: boolean;
  checkoutPhase: CheckoutPhase;
  order: CreateOrderResponse | null;
  capture: CaptureResponse | null;
  checkoutError: string;
  claims: ClaimRecord[];
  claimStatus: ClaimStatus;
  claimError: string;
  selectedClaimId: string | null;
  transactions: TransactionRow[];
  merchants: MerchantTrustRow[];
  summary: LedgerSummary | null;
}

interface ShieldActions {
  bootstrap(): Promise<void>;
  setScenario(id: ScenarioId): Promise<void>;
  toggleOptIn(value: boolean): void;
  beginCheckout(): Promise<CreateOrderResponse | null>;
  approveCapture(paypalOrderId?: string): Promise<void>;
  resetCheckout(): void;
  submitClaim(orderId: string, photo: File): Promise<void>;
  resolveClaim(claimId: string, decision: "REFUND" | "REJECT"): Promise<void>;
  setSelectedClaim(claimId: string | null): void;
  acceptStreamEvent(envelope: StreamEnvelope): void;
}

export type ShieldStore = ShieldState & ShieldActions;

const errText = (e: unknown): string => {
  if (e instanceof ApiError) return e.message;
  if (e instanceof Error) return e.message;
  return String(e);
};

export const useShield = create<ShieldStore>((set, get) => {
  let unsub: (() => void) | null = null;

  function handleEvent(env: StreamEnvelope) {
    acceptStreamEvent(env);
  }

  function acceptStreamEvent(env: StreamEnvelope) {
    switch (env.topic) {
      case "transaction":
        set((s) => ({
          transactions: applyRow(s.transactions, env.record, env.action, "internal_order_id"),
        }));
        void loadLedger();
        break;
      case "claim":
        set((s) => ({
          claims: applyRow(s.claims, env.record, env.action, "claim_id"),
        }));
        void loadLedger();
        break;
      case "merchant":
        set((s) => ({
          merchants: applyRow(s.merchants, env.record, "update", "merchant_id"),
        }));
        break;
    }
  }

  async function loadLedger() {
    const api = get().api;
    if (!api) return;
    try {
      const [transactions, merchants, summary] = await Promise.all([
        api.transactions(),
        api.merchants(),
        api.summary(),
      ]);
      set({ transactions, merchants, summary });
    } catch {
      // Failed ledger network read degrades gracefully
    }
  }

  return {
    api: null,
    mode: "connecting",
    health: null,
    scenario: null,
    assessment: null,
    quote: null,
    quoteStatus: "idle",
    quoteError: "",
    optIn: true,
    checkoutPhase: "idle",
    order: null,
    capture: null,
    checkoutError: "",
    claims: [],
    claimStatus: "idle",
    claimError: "",
    selectedClaimId: null,
    transactions: [],
    merchants: [],
    summary: null,

    async bootstrap() {
      const current = get().api;
      if (current && import.meta.env.MODE === "test") {
        await loadLedger();
        return;
      }
      const api = await createApi();
      unsub?.();
      unsub = api.subscribe(handleEvent);
      let health: HealthStatus | null = null;
      try {
        health = await api.health();
      } catch {
        health = null;
      }
      set({ api, mode: api.mode, health });
      await loadLedger();
    },

    async setScenario(id) {
      const api = get().api;
      if (!api) return;
      const meta = scenarioById(id);
      set({
        scenario: id,
        assessment: null,
        quote: null,
        quoteStatus: "loading",
        quoteError: "",
        checkoutPhase: "idle",
        order: null,
        capture: null,
        checkoutError: "",
        optIn: true,
      });
      try {
        const res: QuoteResponse = await api.quote({
          merchant_id: meta.merchantId,
          item_hash: meta.itemHash,
          item_title: meta.itemTitle,
          item_price: meta.itemPrice,
          currency_code: "USD",
        });
        set({
          assessment: res.assessment,
          quote: res.quote,
          quoteStatus: "ready",
          optIn: res.quote !== null,
        });
      } catch (e) {
        set({ quoteStatus: "error", quoteError: errText(e) });
      }
    },

    toggleOptIn(value) {
      set({ optIn: value });
    },

    async beginCheckout() {
      const { api, quote, scenario, optIn } = get();
      if (!api || !scenario) return null;
      set({
        checkoutPhase: "creating_order",
        checkoutError: "",
        capture: null,
      });
      try {
        const meta = scenarioById(scenario);
        const quoteId = quote?.quote_id ?? `unbacked_${meta.merchantId}`;
        const order = await api.createOrder({
          quote_id: quoteId,
          opt_in: optIn && quote !== null,
          buyer_ref: "demo-buyer",
        } satisfies CreateOrderRequest);
        set({ order, checkoutPhase: "awaiting_approval" });
        return order;
      } catch (e) {
        set({ checkoutPhase: "failed", checkoutError: errText(e) });
        return null;
      }
    },

    async approveCapture(paypalOrderId) {
      const { api, order } = get();
      if (!api || !order) return;
      const orderId = paypalOrderId ?? order.order_id;
      set({ checkoutPhase: "capturing" });
      try {
        const capture = await api.capture(orderId);
        set({ capture, checkoutPhase: "captured" });
      } catch (e) {
        set({ checkoutPhase: "failed", checkoutError: errText(e) });
      }
    },

    resetCheckout() {
      set({ checkoutPhase: "idle", order: null, capture: null, checkoutError: "" });
    },

    async submitClaim(orderId, photo) {
      const { api } = get();
      if (!api) return;
      set({ claimStatus: "uploading", claimError: "" });
      try {
        const claim = await api.submitClaim(orderId, photo);
        set((s) => ({
          claims: applyRow(s.claims, claim, "add", "claim_id"),
          selectedClaimId: claim.claim_id,
          claimStatus: "submitted",
        }));
      } catch (e) {
        set({ claimStatus: "error", claimError: errText(e) });
      }
    },

    async resolveClaim(claimId, decision) {
      const { api } = get();
      if (!api) return;
      try {
        const updated = await api.resolveClaim(claimId, decision, decideReason(decision));
        set((s) => ({ claims: applyRow(s.claims, updated, "update", "claim_id") }));
      } catch (e) {
        set({ claimError: errText(e) });
      }
    },

    setSelectedClaim(claimId) {
      set({ selectedClaimId: claimId });
    },

    acceptStreamEvent(env) {
      handleEvent(env);
    },
  };
});

function decideReason(decision: "REFUND" | "REJECT"): string {
  return decision === "REFUND" ? "REVIEW_REFUNDED" : "REVIEW_REJECTED";
}

export function applyRow<T>(rows: T[], record: T, action: "add" | "update", idField: keyof T & string): T[] {
  const keyOf = (x: T) => x[idField];
  const exists = rows.some((r) => keyOf(r) === keyOf(record));
  if (action === "update" || exists) {
    return rows.map((r) => (keyOf(r) === keyOf(record) ? record : r));
  }
  return [record, ...rows];
}
