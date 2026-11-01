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
  QuoteRequest,
} from "../types/requests";
import type { StreamHandler, Unsubscribe } from "./stream";
import { appConfig } from "../config";

export interface ShieldApi {
  readonly mode: "live" | "mock";
  health(): Promise<HealthStatus>;
  quote(req: QuoteRequest): Promise<QuoteResponse>;
  createOrder(req: CreateOrderRequest): Promise<CreateOrderResponse>;
  capture(orderId: string): Promise<CaptureResponse>;
  submitClaim(orderId: string, photo: File): Promise<ClaimRecord>;
  resolveClaim(
    claimId: string,
    decision: "REFUND" | "REJECT",
    reasonCode: string,
  ): Promise<ClaimRecord>;
  transactions(): Promise<TransactionRow[]>;
  merchants(): Promise<MerchantTrustRow[]>;
  summary(): Promise<LedgerSummary>;
  subscribe(handler: StreamHandler): Unsubscribe;
}

const enc = (v: string) => encodeURIComponent(v);

export class LiveApi implements ShieldApi {
  readonly mode = "live" as const;

  constructor(readonly base: string = appConfig.apiBaseUrl) {}

  private async call<T>(path: string, init?: RequestInit): Promise<T> {
    const res = await fetch(`${this.base}${path}`, init);
    if (!res.ok) {
      const detail = await res.text().catch(() => "");
      let message = `HTTP ${res.status}`;
      try {
        message = JSON.parse(detail).detail ?? message;
      } catch {
        if (detail) message = detail.slice(0, 200);
      }
      throw new ApiError(message, res.status);
    }
    return (await res.json()) as T;
  }

  health() {
    return this.call<HealthStatus>("/api/v1/health");
  }

  quote(req: QuoteRequest) {
    return this.call<QuoteResponse>("/api/v1/quotes", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(req),
    });
  }

  createOrder(req: CreateOrderRequest) {
    return this.call<CreateOrderResponse>("/api/v1/orders", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(req),
    });
  }

  capture(orderId: string) {
    return this.call<CaptureResponse>(
      `/api/v1/orders/${enc(orderId)}/capture`,
      { method: "POST" },
    );
  }

  submitClaim(orderId: string, photo: File) {
    const form = new FormData();
    form.append("order_id", orderId);
    form.append("photo", photo);
    return this.call<ClaimRecord>("/api/v1/claims", { method: "POST", body: form });
  }

  resolveClaim(claimId: string, decision: "REFUND" | "REJECT", reasonCode: string) {
    return this.call<ClaimRecord>(`/api/v1/claims/${enc(claimId)}/resolve`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ decision, reason_code: reasonCode }),
    });
  }

  transactions() {
    return this.call<TransactionRow[]>("/api/v1/ledger/transactions");
  }

  merchants() {
    return this.call<MerchantTrustRow[]>("/api/v1/ledger/merchants");
  }

  summary() {
    return this.call<LedgerSummary>("/api/v1/ledger/summary");
  }

  subscribe(handler: StreamHandler): Unsubscribe {
    const source = new EventSource(`${this.base}/api/v1/stream`);
    const listeners: Array<[string, (ev: MessageEvent) => void]> = [];
    for (const topic of ["transaction", "claim", "merchant"] as const) {
      const fn = (ev: MessageEvent) => {
        try {
          handler(JSON.parse(ev.data));
        } catch {
          return;
        }
      };
      source.addEventListener(topic, fn as EventListener);
      listeners.push([topic, fn]);
    }
    return () => {
      for (const [topic, fn] of listeners) source.removeEventListener(topic, fn as EventListener);
      source.close();
    };
  }
}

export class ApiError extends Error {
  constructor(message: string, readonly status: number) {
    super(message);
    this.name = "ApiError";
  }
}

export async function createApi(): Promise<ShieldApi> {
  const live = new LiveApi();
  let healthy = false;
  try {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 1500);
    const res = await fetch(`${live.base}/api/v1/health`, { signal: controller.signal });
    clearTimeout(timer);
    healthy = res.ok;
  } catch {
    healthy = false;
  }
  if (healthy) return live;
  if (appConfig.allowMock) return (await import("./mock")).sharedMock;
  return live;
}
