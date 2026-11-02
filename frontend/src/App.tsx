import { useEffect, useState } from "react";
import { useShield } from "./state/store";
import { ClaimsPortal } from "./components/ClaimsPortal/ClaimsPortal";
import { CheckoutPanel } from "./components/Storefront/CheckoutPanel";
import { ProductPanel } from "./components/Storefront/ProductPanel";
import { SimulatorDrawer } from "./components/SimulatorDrawer/SimulatorDrawer";
import { LedgerGrid } from "./components/LedgerGrid/LedgerGrid";

type Tab = "storefront" | "claims" | "ledger";

const tabs: Array<{ id: Tab; label: string }> = [
  { id: "storefront", label: "Storefront" },
  { id: "claims", label: "Claims portal" },
  { id: "ledger", label: "Ledger" },
];

export function App() {
  const bootstrap = useShield((s) => s.bootstrap);
  const mode = useShield((s) => s.mode);
  const health = useShield((s) => s.health);
  const claimsCount = useShield((s) => s.claims.length);
  const [tab, setTab] = useState<Tab>("storefront");

  useEffect(() => {
    void bootstrap();
  }, [bootstrap]);

  return (
    <div className="mx-auto min-h-screen max-w-7xl px-4 pb-10">
      <header className="flex items-center justify-between py-4">
        <div className="flex items-center gap-3">
          <img src="/shield.svg" alt="PayPal Shield logo" className="h-9 w-9" />
          <div>
            <p className="text-base font-extrabold tracking-tight text-shield-navy">PayPal Shield</p>
            <p className="text-[11px] text-slate-500">Real-time risk evaluation and buyer protection for PayPal Checkout</p>
          </div>
        </div>
        <span
          data-testid="mode-chip"
          className="rounded-full bg-white px-3 py-1 text-[11px] font-semibold text-slate-600 ring-1 ring-slate-200"
        >
          {mode === "connecting"
            ? "connecting..."
            : mode === "mock"
              ? "sandbox demo mode"
              : health?.paypal_sandbox_connected
                ? "PayPal Sandbox connected"
                : "live API"}
        </span>
      </header>

      <nav className="flex gap-1" aria-label="Main">
        {tabs.map((t) => (
          <button
            key={t.id}
            type="button"
            data-testid={`tab-${t.id}`}
            aria-current={tab === t.id}
            onClick={() => setTab(t.id)}
            className={`rounded-t-lg border-b-2 px-4 py-2 text-sm font-semibold transition ${
              tab === t.id ? "border-blue-600 text-blue-700" : "border-transparent text-slate-500 hover:text-slate-800"
            }`}
          >
            {t.label}
            {t.id === "claims" && claimsCount > 0 ? (
              <span className="ml-1.5 rounded-full bg-slate-200 px-1.5 text-[10px] text-slate-700">{claimsCount}</span>
            ) : null}
          </button>
        ))}
      </nav>

      {tab === "storefront" && (
        <div className="mt-4 flex flex-col-reverse gap-6 lg:flex-row">
          <SimulatorDrawer />
          <div className="flex-1 space-y-6">
            <ProductPanel />
            <CheckoutPanel />
          </div>
        </div>
      )}

      {tab === "claims" && (
        <div className="mt-4">
          <ClaimsPortal />
        </div>
      )}

      {tab === "ledger" && (
        <div className="mt-4">
          <LedgerGrid />
        </div>
      )}

      <footer className="mt-8 border-t border-slate-200 pt-4 text-center">
        <p className="text-[11px] leading-5 text-slate-400">
          The Buyer Protection Fee is a platform service fee, not a regulated product. Commercial operation requires a
          partnership with a licensed underwriter. Demo build by the PayPal Shield team.
        </p>
      </footer>
    </div>
  );
}
