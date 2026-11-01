import { describe, it, expect } from "vitest";
import { sharedMock, toCents, fromCents } from "../api/mock";
import type { QuoteRequest, CreateOrderRequest } from "../types/requests";

const quoteReq = (merchant_id: string, item_price: string): QuoteRequest => ({
  merchant_id,
  item_hash: "",
  item_title: "demo",
  item_price,
  currency_code: "USD",
});

const orderReq = (quote_id: string): CreateOrderRequest => ({
  quote_id,
  opt_in: true,
  buyer_ref: "demo-buyer",
});

describe("mock api worked examples (spec 6.3)", () => {
  it("trusted scenario quotes the verified fee", async () => {
    const res = await sharedMock.quote(quoteReq("mer_audio_den", "120.00"));
    expect(res.assessment.trust_score).toBe("0.9381");
    expect(res.assessment.r_base).toBe("0.0780");
    expect(res.assessment.r_final).toBe("7.80");
    expect(res.assessment.gate).toBe("COVERAGE_AVAILABLE");
    expect(res.quote?.protection_fee).toBe("0.47");
  });

  it("AI-scam scenario quotes 2.81", async () => {
    const res = await sharedMock.quote(quoteReq("mer_dash_deals", "80.00"));
    expect(res.assessment.r_final).toBe("70.25");
    expect(res.quote?.protection_fee).toBe("2.81");
    expect(res.assessment.pillars.is_ai_generated).toBe(true);
  });

  it("repeat offender is blocked with no quote", async () => {
    const res = await sharedMock.quote(quoteReq("mer_refurb_find", "60.00"));
    expect(res.assessment.r_final).toBe("81.00");
    expect(res.assessment.gate).toBe("BLOCKED_HIGH_RISK");
    expect(res.quote).toBeNull();
  });
});

describe("checkout flow", () => {
  it("creates and captures an opted-in order with the fee item", async () => {
    const res = await sharedMock.quote(quoteReq("mer_audio_den", "120.00"));
    const order = await sharedMock.createOrder(orderReq(res.quote!.quote_id));
    expect(order.total).toBe("120.47");
    const capture = await sharedMock.capture(order.internal_order_id);
    expect(capture.status).toBe("COMPLETED");
    expect(capture.paypal_capture_id).toBeTruthy();
  });

  it("stream broadcast reports the capture as a transaction", async () => {
    const captured: unknown[] = [];
    sharedMock.subscribe((env) => {
      if (env.topic === "transaction") captured.push(env.record);
    });
    const res = await sharedMock.quote(quoteReq("mer_dash_deals", "80.00"));
    const order = await sharedMock.createOrder(orderReq(res.quote!.quote_id));
    await sharedMock.capture(order.internal_order_id);
    expect(captured.length).toBeGreaterThan(0);
  });
});

describe("claim pipeline", () => {
  it("advances a scam-order claim to REFUNDED out-of-band", async () => {
    const states: string[] = [];
    sharedMock.subscribe((env) => {
      if (env.topic === "claim") states.push(env.record.state);
    });
    const res = await sharedMock.quote(quoteReq("mer_dash_deals", "80.00"));
    const order = await sharedMock.createOrder(orderReq(res.quote!.quote_id));
    await sharedMock.capture(order.internal_order_id);
    const file = new File(["photo"], "item.png", { type: "image/png" });
    await sharedMock.submitClaim(order.internal_order_id, file);
    await new Promise((r) => setTimeout(r, 4600));
    expect(states.slice(-3)).toEqual(["EVIDENCE_CHECKED", "AUTO_APPROVED", "REFUNDED"]);
    sharedMock.stop();
  }, 15000);
});

describe("integer money helpers", () => {
  it("adds cents exactly", () => {
    expect(fromCents(toCents("120.47") + toCents("0.47"))).toBe("120.94");
    expect(fromCents(toCents("0.10") + toCents("0.20"))).toBe("0.30");
  });
});
