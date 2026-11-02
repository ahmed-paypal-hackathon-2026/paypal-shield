import type { LedgerSummary } from "../../types/contracts";

export interface PinnedTotalsRow {
  merchant_id: string;
  item_price: string;
  protection_fee: string;
  r_final: string;
  state: string;
  created_at: string;
}

export function buildPinnedTotalsRow(summary: LedgerSummary | null): PinnedTotalsRow {
  if (!summary) {
    return {
      merchant_id: "Network totals",
      item_price: "—",
      protection_fee: "—",
      r_final: "—",
      state: "—",
      created_at: "",
    };
  }
  return {
    merchant_id: "Network totals",
    item_price: summary.total_volume,
    protection_fee: summary.protection_fees_collected,
    r_final: summary.net_loss_ratio,
    state: `Refunds issued ${summary.refunds_issued}`,
    created_at: "",
  };
}

export function totalsRowId(row: PinnedTotalsRow): string {
  return row.merchant_id;
}
