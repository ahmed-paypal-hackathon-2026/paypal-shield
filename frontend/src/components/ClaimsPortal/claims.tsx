import type { ClaimRecord, ClaimState } from "../../types/contracts";
import { formatMoney, formatTimestamp } from "../../utils/format";

const steps: ClaimState[] = [
  "SUBMITTED",
  "EVIDENCE_CHECKED",
  "AUTO_APPROVED",
  "MANUAL_REVIEW",
  "REFUNDED",
  "REJECTED",
];

const labels: Record<ClaimState, string> = {
  SUBMITTED: "Submitted",
  EVIDENCE_CHECKED: "Evidence checked",
  AUTO_APPROVED: "Auto-approved",
  MANUAL_REVIEW: "Manual review",
  REFUNDED: "Refunded",
  REJECTED: "Rejected",
};

export function claimProgress(state: ClaimState): number {
  return steps.indexOf(state);
}

const stateClasses: Record<ClaimState, string> = {
  SUBMITTED: "bg-slate-100 text-slate-700 ring-slate-500/20",
  EVIDENCE_CHECKED: "bg-blue-50 text-blue-700 ring-blue-600/20",
  AUTO_APPROVED: "bg-emerald-50 text-emerald-700 ring-emerald-600/20",
  MANUAL_REVIEW: "bg-amber-50 text-amber-700 ring-amber-600/20",
  REFUNDED: "bg-emerald-100 text-emerald-800 ring-emerald-600/30",
  REJECTED: "bg-red-50 text-red-700 ring-red-600/20",
};

export function ClaimCard({
  claim,
  selected,
  onSelect,
}: {
  claim: ClaimRecord;
  selected: boolean;
  onSelect: (id: string) => void;
}) {
  const progress = claimProgress(claim.state);
  return (
    <button
      type="button"
      onClick={() => onSelect(claim.claim_id)}
      className={`w-full rounded-xl border p-4 text-left transition ${
        selected ? "border-blue-400 ring-1 ring-blue-400" : "border-slate-200 hover:border-slate-300"
      }`}
      data-testid={`claim-card-${claim.claim_id}`}
    >
      <div className="flex items-center justify-between gap-2">
        <span className={`rounded-full px-2.5 py-1 text-[11px] font-semibold ring-1 ring-inset ${stateClasses[claim.state]}`}>
          {labels[claim.state]}
        </span>
        <span className="text-xs font-semibold text-slate-700">
          {formatMoney(claim.claim_amount, claim.currency_code)}
        </span>
      </div>
      <div className="mt-2 flex items-center gap-1 text-[11px] text-slate-400">
        order <span className="font-mono">{claim.order_id}</span> · {formatTimestamp(claim.updated_at)}
        {claim.refund_id ? (
          <>
            {" · refund "}
            <span className="font-mono text-emerald-700">{claim.refund_id}</span>
          </>
        ) : null}
      </div>
      {progress >= 0 && (<Stepper state={claim.state} />)}
    </button>
  );
}

function Stepper({ state }: { state: ClaimState }) {
  if (state === "REJECTED") {
    return (
      <div className="mt-2 flex items-center gap-1" aria-label="claim progress">
        <Dot done />
        <Bar done />
        <Dot done />
        <Bar done />
        <span className="text-[10px] font-semibold uppercase tracking-wide text-red-500">Rejected</span>
      </div>
    );
  }
  const idx = claimProgress(state);
  return (
    <div className="mt-2 flex items-center gap-1" aria-label="claim progress">
      {steps.slice(0, 5).map((step, i) => (
        <span key={step} className="flex items-center gap-1">
          {i > 0 ? <Bar done={i <= idx} /> : null}
          <Dot done={i <= idx || (state === "MANUAL_REVIEW" && step === "MANUAL_REVIEW")} />
        </span>
      ))}
    </div>
  );
}

function Dot({ done }: { done: boolean }) {
  return (
    <span
      className={`h-2 w-2 shrink-0 rounded-full ${done ? "bg-emerald-500" : "bg-slate-200"}`}
      aria-hidden
    />
  );
}

function Bar({ done }: { done: boolean }) {
  return <span className={`h-0.5 w-4 shrink-0 rounded ${done ? "bg-emerald-400" : "bg-slate-200"}`} aria-hidden />;
}
