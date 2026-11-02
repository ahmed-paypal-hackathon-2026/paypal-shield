import { useEffect, useMemo, useRef, useState } from "react";
import { AgGridReact } from "ag-grid-react";
import {
  themeQuartz,
  type ColDef,
  type CellClassParams,
  type GridApi,
  type ICellRendererParams,
  type RowDataTransaction,
} from "ag-grid-community";
import type { MerchantTrustRow, TransactionRow } from "../../types/contracts";
import { useShield } from "../../state/store";
import { formatTimestamp } from "../../utils/format";
import { RiskBadge } from "../RiskBadge/RiskBadge";
import { buildPinnedTotalsRow } from "./totals";
import { compareDecimals } from "../RiskBadge/bands";

const statePalette: Record<string, string> = {
  CAPTURED: "bg-emerald-50 text-emerald-700",
  QUOTED: "bg-slate-100 text-slate-600",
  REFUNDED: "bg-orange-50 text-orange-700",
  PARTIALLY_REFUNDED: "bg-amber-50 text-amber-700",
};

function stateClass(state: string): string {
  return statePalette[state] ?? "bg-slate-100 text-slate-700";
}

function RiskCell(props: ICellRendererParams<TransactionRow>) {
  const value = props.value as string;
  if (!value) return <span className="text-slate-300">—</span>;
  return <RiskBadge rFinal={value} size="sm" />;
}

function StateCell(props: ICellRendererParams<TransactionRow>) {
  const state = (props.value as string) ?? "";
  return (
    <span className={`inline-block rounded px-2 py-0.5 text-[11px] font-semibold ${stateClass(state)}`}>
      {state}
    </span>
  );
}

function TimestampCell(props: ICellRendererParams<TransactionRow>) {
  const v = props.value as string;
  return <span className="tabular-nums">{v ? formatTimestamp(v) : ""}</span>;
}

function TrustCell(props: ICellRendererParams<MerchantTrustRow>) {
  const v = props.value as string;
  const good = v ? compareDecimals(v, "0.8500") >= 0 : false;
  return (
    <span className="flex items-center gap-2">
      <span className={`h-1.5 w-1.5 rounded-full ${good ? "bg-emerald-500" : "bg-amber-500"}`} />
      <span className="font-mono">{v}</span>
    </span>
  );
}

function PenaltyCell(props: ICellRendererParams<MerchantTrustRow>) {
  const v = props.value as string;
  const severe = v === "1.50" || v === "2.00";
  return (
    <span className={`rounded px-2 py-0.5 text-[11px] font-semibold ${severe ? "bg-red-50 text-red-700" : "bg-slate-100 text-slate-600"}`}>
      × {v}
    </span>
  );
}

const transactionColumns: ColDef<TransactionRow>[] = [
  {
    field: "merchant_id",
    headerName: "Merchant",
    flex: 1.4,
    cellRenderer: (p: ICellRendererParams<TransactionRow>) => <span className="font-mono text-xs">{p.value}</span>,
  },
  {
    field: "internal_order_id",
    headerName: "Order",
    flex: 1.2,
    cellRenderer: (p: ICellRendererParams<TransactionRow>) => (
      <span className="font-mono text-xs text-slate-500">{p.value}</span>
    ),
  },
  { field: "item_price", headerName: "Item price", type: "rightAligned", valueFormatter: (p) => String(p.value) },
  {
    field: "protection_fee",
    headerName: "Protection fee",
    type: "rightAligned",
    valueFormatter: (p) => String(p.value),
  },
  { field: "r_final", headerName: "Closing risk", cellRenderer: RiskCell },
  { field: "state", headerName: "State", cellRenderer: StateCell },
  { field: "created_at", headerName: "When", cellRenderer: TimestampCell },
];

const merchantColumns: ColDef<MerchantTrustRow>[] = [
  {
    field: "merchant_id",
    headerName: "Merchant",
    flex: 1.4,
    cellRenderer: (p: ICellRendererParams<MerchantTrustRow>) => <span className="font-mono text-xs">{p.value}</span>,
  },
  { field: "total_orders", headerName: "Total orders", type: "rightAligned" },
  { field: "successful_orders", headerName: "Successful", type: "rightAligned" },
  {
    field: "verified_fraud_claims",
    headerName: "Verified fraud",
    type: "rightAligned",
    cellClass: (p: CellClassParams<MerchantTrustRow>) => ((p.value as number) > 0 ? "text-red-600 font-semibold" : "text-slate-500"),
  },
  { field: "trust_score", headerName: "Trust score", cellRenderer: TrustCell },
  { field: "m_penalty", headerName: "Penalty", cellRenderer: PenaltyCell },
];

export function LedgerGrid() {
  const transactions = useShield((s) => s.transactions);
  const merchants = useShield((s) => s.merchants);
  const summary = useShield((s) => s.summary);

  const [tab, setTab] = useState<"transactions" | "merchants">("transactions");
  const transactionGrid = useRef<GridApi<TransactionRow> | null>(null);
  const [pinnedRow, setPinnedRow] = useState(() => buildPinnedTotalsRow(null));
  const previousTransactions = useRef<TransactionRow[]>([]);

  useEffect(() => {
    setPinnedRow(buildPinnedTotalsRow(summary));
  }, [summary]);

  useEffect(() => {
    setPinnedRow(buildPinnedTotalsRow(summary));
  }, [transactions.length, summary]);

  useEffect(() => {
    const gridApi = transactionGrid.current;
    if (!gridApi) return;
    const tx = diffRows(previousTransactions.current, transactions, "internal_order_id");
    if ((tx.add?.length ?? 0) + (tx.update?.length ?? 0) + (tx.remove?.length ?? 0) > 0) {
      gridApi.applyTransaction(tx);
    }
    previousTransactions.current = transactions;
  }, [transactions]);

  useEffect(() => {
    transactionGrid.current?.setGridOption("pinnedBottomRowData", [pinnedRow]);
  }, [pinnedRow, tab]);

  const initialRowData = useMemo(() => previousTransactions.current, []);
  const merchantsRowData = useMemo(() => merchants, [merchants]);

  return (
    <div className="flex-1">
      <div className="mb-3 flex items-center gap-2">
        {(
          [
            ["transactions", "Transactions ledger"],
            ["merchants", "Merchant trust registry"],
          ] as const
        ).map(([key, label]) => (
          <button
            key={key}
            type="button"
            data-testid={`ledger-tab-${key}`}
            onClick={() => setTab(key)}
            className={`rounded-full px-4 py-1.5 text-xs font-semibold transition ${
              tab === key ? "bg-slate-900 text-white" : "bg-white text-slate-600 hover:bg-slate-50 ring-1 ring-slate-200"
            }`}
          >
            {label}
          </button>
        ))}
      </div>

      <div
        data-testid="ledger-grid"
        className={`overflow-hidden rounded-xl border border-slate-200 shadow-card ${
          tab === "transactions" ? "block" : "hidden"
        }`}
      >
        <AgGridReact<TransactionRow>
          onGridReady={(p) => {
            transactionGrid.current = p.api;
            p.api.setGridOption("pinnedBottomRowData", [buildPinnedTotalsRow(summary)]);
          }}
          getRowId={(p) => p.data.internal_order_id}
          columnDefs={transactionColumns}
          rowData={initialRowData}
          pinnedBottomRowData={[pinnedRow]}
          theme={themeQuartz.withParams({
            accentColor: "#0070e0",
            borderRadius: 12,
            headerHeight: 40,
            rowHeight: 44,
            fontFamily: "Inter, system-ui, sans-serif",
          })}
          animateRows
          domLayout="autoHeight"
        />
      </div>

      <div
        className={`overflow-hidden rounded-xl border border-slate-200 shadow-card ${tab === "merchants" ? "block" : "hidden"}`}
      >
        <AgGridReact<MerchantTrustRow>
          getRowId={(p) => p.data.merchant_id}
          columnDefs={merchantColumns}
          rowData={merchantsRowData}
          theme={themeQuartz.withParams({
            accentColor: "#0070e0",
            borderRadius: 12,
            headerHeight: 40,
            rowHeight: 44,
            fontFamily: "Inter, system-ui, sans-serif",
          })}
          animateRows
          domLayout="autoHeight"
        />
      </div>

      <p className="mt-2 text-xs text-slate-500" data-testid="ledger-summary-line">
        Total volume {summary?.total_volume ?? "—"} · Protection fees {summary?.protection_fees_collected ?? "—"} ·
        Refunds {summary?.refunds_issued ?? "—"} · Net loss ratio {summary?.net_loss_ratio ?? "—"}
      </p>
    </div>
  );
}

/** Computes add/update/remove transactions between two snapshots, keyed by id. */
export function diffRows<T>(
  previous: T[],
  next: T[],
  idField: keyof T & string,
): RowDataTransaction<T> {
  const keyOf = (x: T) => String(x[idField]);
  const prevIds = new Map(previous.map((r) => [keyOf(r), r]));
  const nextIds = new Map(next.map((r) => [keyOf(r), r]));
  const add: T[] = [];
  const update: T[] = [];
  const remove: T[] = [];
  for (const row of next) {
    const prior = prevIds.get(keyOf(row));
    if (!prior) add.push(row);
    else if (prior !== row && JSON.stringify(prior) !== JSON.stringify(row)) update.push(row);
  }
  for (const row of previous) {
    if (!nextIds.has(keyOf(row))) remove.push(row);
  }
  return { add, update, remove };
}
