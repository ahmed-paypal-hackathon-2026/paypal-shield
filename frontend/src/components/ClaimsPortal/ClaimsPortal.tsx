import { useEffect, useRef, useState } from "react";
import { useShield } from "../../state/store";
import { ClaimCard } from "./claims";

export function ClaimsPortal() {
  const claims = useShield((s) => s.claims);
  const selectedClaimId = useShield((s) => s.selectedClaimId);
  const setSelectedClaim = useShield((s) => s.setSelectedClaim);
  const transactions = useShield((s) => s.transactions);
  const claimStatus = useShield((s) => s.claimStatus);
  const claimError = useShield((s) => s.claimError);
  const submitClaim = useShield((s) => s.submitClaim);
  const resolveClaim = useShield((s) => s.resolveClaim);
  const fileInput = useRef<HTMLInputElement>(null);
  const [selectedOrder, setSelectedOrder] = useState<string>("");

  const captured = transactions.filter((t) => t.state === "CAPTURED");

  useEffect(() => {
    if (!selectedOrder && captured.length > 0) setSelectedOrder(captured[0].internal_order_id);
  }, [captured, selectedOrder]);

  const selected = claims.find((c) => c.claim_id === selectedClaimId) ?? null;

  return (
    <div className="flex flex-1 flex-wrap gap-6">
      <section className="min-w-72 flex-1 rounded-xl border border-slate-200 bg-white p-6 shadow-card">
        <h2 className="text-sm font-bold text-slate-800">New counterfeit claim</h2>
        <p className="mt-1 text-xs text-slate-500">
          Upload a photo of the received item for a captured order. The arbiter compares it against the
          listing photo and applies the payout gates.
        </p>
        <div className="mt-4 space-y-3">
          <div>
            <label htmlFor="claim-order" className="mb-1 block text-xs font-medium text-slate-600">
              Captured order
            </label>
            <select
              id="claim-order"
              data-testid="claim-order-select"
              value={selectedOrder}
              onChange={(e) => setSelectedOrder(e.target.value)}
              className="w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm text-slate-800 focus:border-blue-500 focus:outline-none"
            >
              {captured.length === 0 && <option value="">No captured orders yet</option>}
              {captured.map((t) => (
                <option key={t.internal_order_id} value={t.internal_order_id}>
                  {t.internal_order_id} — {t.merchant_id} ({t.item_price})
                </option>
              ))}
            </select>
          </div>

          <div
            data-testid="photo-drop"
            onDragOver={(e) => e.preventDefault()}
            onDrop={(e) => {
              e.preventDefault();
              if (fileInput.current && e.dataTransfer.files.length > 0) {
                const dt = new DataTransfer();
                dt.items.add(e.dataTransfer.files[0]);
                fileInput.current.files = dt.files;
              }
            }}
            className="flex items-center justify-center rounded-lg border-2 border-dashed border-slate-300 bg-slate-50 p-6 text-center"
          >
            <div>
              <p className="text-xs font-medium text-slate-600">Item photo (jpeg, png or webp)</p>
              <p className="mt-0.5 text-[11px] text-slate-400">Validated server-side by magic bytes</p>
            </div>
          </div>

          <input
            ref={fileInput}
            type="file"
            accept="image/jpeg,image/png,image/webp"
            data-testid="photo-input"
            className="w-full text-xs text-slate-600 file:mr-3 file:rounded-md file:border-0 file:bg-slate-900 file:px-3 file:py-1.5 file:text-xs file:font-semibold file:text-white hover:file:bg-slate-700"
          />

          <button
            type="button"
            data-testid="submit-claim"
            disabled={!selectedOrder || claimStatus === "uploading"}
            onClick={() => {
              const file = fileInput.current?.files?.[0];
              if (file && selectedOrder) void submitClaim(selectedOrder, file);
            }}
            className="w-full rounded-lg bg-blue-600 px-4 py-2 text-sm font-semibold text-white hover:bg-blue-500 disabled:opacity-50"
          >
            {claimStatus === "uploading" ? "Submitting..." : "File claim"}
          </button>

          {claimError && (
            <p data-testid="claim-error" className="rounded-lg bg-red-50 p-3 text-xs text-red-700">
              {claimError}
            </p>
          )}
        </div>
      </section>

      <section className="min-w-80 flex-[1.4]">
        <h2 className="mb-3 text-sm font-bold text-slate-800">Claims</h2>
        <div className="space-y-3">
          {claims.length === 0 && (
            <p className="rounded-xl border border-dashed border-slate-300 bg-white/60 p-8 text-center text-xs text-slate-500">
              No claims yet. State changes appear here live.
            </p>
          )}
          {claims.map((c) => (
            <ClaimCard
              key={c.claim_id}
              claim={c}
              selected={c.claim_id === selectedClaimId}
              onSelect={setSelectedClaim}
            />
          ))}
        </div>

        {selected && selected.state === "MANUAL_REVIEW" && (
          <div className="mt-4 rounded-xl border border-amber-200 bg-amber-50 p-4">
            <p className="text-xs font-semibold text-amber-800">Reviewer decision (admin)</p>
            <div className="mt-2 flex gap-2">
              <button
                type="button"
                data-testid="resolve-refund"
                onClick={() => void resolveClaim(selected.claim_id, "REFUND")}
                className="rounded-md bg-emerald-600 px-3 py-1.5 text-xs font-semibold text-white hover:bg-emerald-500"
              >
                Approve refund
              </button>
              <button
                type="button"
                data-testid="resolve-reject"
                onClick={() => void resolveClaim(selected.claim_id, "REJECT")}
                className="rounded-md bg-white px-3 py-1.5 text-xs font-semibold text-red-600 ring-1 ring-red-200 hover:bg-red-50"
              >
                Reject claim
              </button>
            </div>
          </div>
        )}
      </section>
    </div>
  );
}
