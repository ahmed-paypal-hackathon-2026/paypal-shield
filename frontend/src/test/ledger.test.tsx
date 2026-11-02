import { describe, it, expect } from "vitest";
import { buildPinnedTotalsRow } from "../components/LedgerGrid/totals";
import { diffRows } from "../components/LedgerGrid/LedgerGrid";
import type { LedgerSummary, TransactionRow } from "../types/contracts";

const summary: LedgerSummary = {
  total_volume: "200.47",
  protection_fees_collected: "3.28",
  refunds_issued: "80.00",
  net_loss_ratio: "0.0500",
};

const row = (id: string): TransactionRow => ({
  internal_order_id: id,
  merchant_id: "mer_audio_den",
  item_price: "120.00",
  protection_fee: "0.47",
  r_final: "7.80",
  state: "CAPTURED",
  created_at: "2026-01-01T00:00:00Z",
});

describe("pinned totals row", () => {
  it("maps the four summary metrics", () => {
    const pinned = buildPinnedTotalsRow(summary);
    expect(pinned.merchant_id).toBe("Network totals");
    expect(pinned.item_price).toBe("200.47");
    expect(pinned.protection_fee).toBe("3.28");
    expect(pinned.r_final).toBe("0.0500");
    expect(pinned.state).toContain("80.00");
  });

  it("prints a placeholder without data", () => {
    const pinned = buildPinnedTotalsRow(null);
    expect(pinned.item_price).toBe("—");
  });
});

describe("grid live updates via diffRows", () => {
  it("detects adds and updates for applyTransaction", () => {
    const tx = diffRows([row("o1")], [row("o1"), row("o2")], "internal_order_id");
    expect((tx.add ?? []).map((r) => r.internal_order_id)).toEqual(["o2"]);
    expect(tx.update).toHaveLength(0);

    const updated = { ...row("o1"), state: "REFUNDED" };
    const tx2 = diffRows([row("o1")], [updated], "internal_order_id");
    expect((tx2.update ?? []).map((r) => r.internal_order_id)).toEqual(["o1"]);
  });
});

describe("pinned row id", () => {
  it("is stable under getRowId", () => {
    expect(buildPinnedTotalsRow(summary).merchant_id).toBe("Network totals");
  });
});
