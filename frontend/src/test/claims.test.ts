import { describe, it, expect } from "vitest";
import { applyRow } from "../state/store";
import type { ClaimRecord } from "../types/contracts";

const claim = (id: string, state: ClaimRecord["state"]): ClaimRecord => ({
  claim_id: id,
  order_id: "ord_" + id,
  state,
  claim_amount: "80.00",
  currency_code: "USD",
  updated_at: "2026-01-01T00:00:00Z",
});

describe("claim lifecycle reducer", () => {
  it("appends NEW claims on add", () => {
    const rows: ClaimRecord[] = [claim("a", "SUBMITTED")];
    const next = applyRow(rows, claim("b", "EVIDENCE_CHECKED"), "add", "claim_id");
    expect(next.map((c) => c.claim_id)).toEqual(["b", "a"]);
  });

  it("upserts in place on update", () => {
    const rows = [claim("a", "SUBMITTED"), claim("b", "EVIDENCE_CHECKED")];
    const next = applyRow(rows, claim("a", "AUTO_APPROVED"), "update", "claim_id");
    expect(next.length).toBe(2);
    expect(next.find((c) => c.claim_id === "a")?.state).toBe("AUTO_APPROVED");
  });

  it("add of an existing id replaces the row", () => {
    const rows = [claim("a", "SUBMITTED"), claim("b", "MANUAL_REVIEW")];
    const next = applyRow(rows, claim("a", "REFUNDED"), "add", "claim_id");
    expect(next.length).toBe(2);
    expect(next.find((c) => c.claim_id === "a")?.state).toBe("REFUNDED");
  });
});
