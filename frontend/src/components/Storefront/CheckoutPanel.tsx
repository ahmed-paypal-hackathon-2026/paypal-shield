import { useEffect, useMemo } from "react";
import { PayPalButtons, PayPalScriptProvider } from "@paypal/react-paypal-js";
import { useShield } from "../../state/store";
import { appConfig } from "../../config";
import { formatMoney } from "../../utils/format";

function phaseLabel(phase: string): string | null {
  switch (phase) {
    case "creating_order":
      return "Creating PayPal order...";
    case "awaiting_approval":
      return "Approving payment...";
    case "capturing":
      return "Capturing payment...";
    default:
      return null;
  }
}

export function CheckoutPanel() {
  const scenario = useShield((s) => s.scenario);
  const quote = useShield((s) => s.quote);
  const optIn = useShield((s) => s.optIn);
  const toggleOptIn = useShield((s) => s.toggleOptIn);
  const checkoutPhase = useShield((s) => s.checkoutPhase);
  const checkoutError = useShield((s) => s.checkoutError);
  const order = useShield((s) => s.order);
  const capture = useShield((s) => s.capture);
  const beginCheckout = useShield((s) => s.beginCheckout);
  const approveCapture = useShield((s) => s.approveCapture);
  const resetCheckout = useShield((s) => s.resetCheckout);
  const mode = useShield((s) => s.mode);

  const currency = quote?.currency_code ?? "USD";
  const blocked = quote === null && scenario !== null;
  const busy = checkoutPhase === "creating_order" || checkoutPhase === "capturing";

  const scriptOptions = useMemo(
    () => ({
      clientId: appConfig.paypalClientId ?? "test",
      currency: "USD",
      intent: "capture",
      components: "buttons",
    }),
    [],
  );

  useEffect(() => {
    if (mode === "mock" && checkoutPhase === "awaiting_approval") {
      const id = window.setTimeout(() => void approveCapture(), 600);
      return () => window.clearTimeout(id);
    }
  }, [mode, checkoutPhase, approveCapture]);

  if (!scenario) return null;

  return (
    <section className="w-full rounded-xl border border-slate-200 bg-white p-6 shadow-card" data-testid="checkout-panel">
      <h2 className="text-sm font-bold text-slate-800">Protection Plan checkout</h2>

      {blocked ? (
        <div data-testid="blocked-panel" className="mt-3 rounded-lg border border-red-200 bg-red-50 p-4">
          <p className="text-sm font-semibold text-red-700">
            The risk score for this listing exceeds the coverage gate.
          </p>
          <p className="mt-1 text-xs leading-5 text-red-600">
            Checkout still works, but it proceeds without a Protection Plan and without any coverage.
          </p>
          <div className="mt-4">
            {checkoutPhase === "captured" && capture ? (
              <ResultBox capture={capture} total={order?.total ?? ""} currency={currency} onReset={resetCheckout} />
            ) : (
              <button
                type="button"
                data-testid="checkout-without-protection"
                disabled={busy}
                onClick={() => void beginCheckout()}
                className="rounded-lg bg-slate-900 px-4 py-2 text-sm font-semibold text-white hover:bg-slate-800 disabled:opacity-50"
              >
                {phaseLabel(checkoutPhase) ?? "Buy without coverage"}
              </button>
            )}
          </div>
        </div>
      ) : (
        quote && (
          <div className="mt-3">
            <label className="flex items-start gap-3 rounded-lg bg-slate-50 p-4" data-testid="opt-in-row">
              <input
                type="checkbox"
                data-testid="opt-in-checkbox"
                checked={optIn}
                onChange={(e) => toggleOptIn(e.target.checked)}
                className="mt-0.5 h-4 w-4 accent-blue-600"
              />
              <span>
                <span className="block text-sm font-semibold text-slate-800">
                  Add the Buyer Protection Fee {formatMoney(quote.protection_fee, currency)}
                </span>
                <span className="mt-0.5 block text-xs leading-5 text-slate-500">
                  The Protection Plan covers counterfeit claims: upload a photo of the received item and PayPal
                  Shield's claim arbiter reviews the discrepancy. Valid claims are refunded automatically.
                </span>
              </span>
            </label>

            <div className="mt-4">
              {checkoutPhase === "captured" && capture ? (
                <ResultBox capture={capture} total={order?.total ?? ""} currency={currency} onReset={resetCheckout} />
              ) : mode === "mock" ? (
                <div className="flex items-center gap-3">
                  <button
                    type="button"
                    data-testid="checkout-button"
                    disabled={checkoutPhase === "awaiting_approval" || busy}
                    onClick={() => void beginCheckout()}
                    className="rounded-lg bg-amber-400 px-4 py-2 text-sm font-bold text-slate-900 hover:bg-amber-300 disabled:opacity-50"
                  >
                    {phaseLabel(checkoutPhase) ?? "Check out with PayPal (demo)"}
                  </button>
                  <span className="text-xs text-slate-500">sandbox demo mode</span>
                </div>
              ) : (
                <PayPalScriptProvider options={scriptOptions}>
                  <PayPalButtons
                    style={{ layout: "horizontal", label: "paypal", shape: "pill" }}
                    createOrder={async () => {
                      const resp = await beginCheckout();
                      return resp?.order_id ?? "";
                    }}
                    onApprove={async (data) => {
                      await approveCapture(data.orderID);
                    }}
                    onError={() => {
                      void 0;
                    }}
                  />
                </PayPalScriptProvider>
              )}
            </div>

            {checkoutError && (
              <p data-testid="checkout-error" className="mt-3 rounded-lg bg-red-50 p-3 text-xs text-red-700">
                {checkoutError}
              </p>
            )}
          </div>
        )
      )}
    </section>
  );
}

function ResultBox({
  capture,
  total,
  currency,
  onReset,
}: {
  capture: { order_id: string; paypal_capture_id: string; status: string };
  total: string;
  currency: string;
  onReset: () => void;
}) {
  return (
    <div data-testid="capture-result" className="rounded-lg border border-emerald-200 bg-emerald-50 p-4">
      <p className="text-sm font-semibold text-emerald-700">{capture.status === "COMPLETED" ? "Paid" : capture.status}</p>
      <p className="mt-1 text-xs text-emerald-700">
        Capture id <span className="font-mono">{capture.paypal_capture_id}</span> · total{" "}
        {formatMoney(total, currency)}
      </p>
      <button
        type="button"
        onClick={onReset}
        className="mt-3 rounded-md border border-emerald-300 bg-white px-3 py-1.5 text-xs font-semibold text-emerald-700 hover:bg-emerald-100"
      >
        Run the scenario again
      </button>
    </div>
  );
}
