import { useShield } from "../../state/store";
import { RiskBadge, ReasonChips } from "../RiskBadge/RiskBadge";
import { formatMoney } from "../../utils/format";
import { scenarioById } from "../../data/scenarios";

function PillarRow({ label, value }: { label: string; value: string }) {
  const strong = value.startsWith("0.0");
  return (
    <div className="flex items-center justify-between text-xs" data-testid={`pillar-${label}`}>
      <span className="text-slate-500">{label}</span>
      <span className={`font-mono ${strong ? "text-emerald-600" : "text-amber-700"}`}>{value}</span>
    </div>
  );
}

export function ProductPanel() {
  const scenario = useShield((s) => s.scenario);
  const assessment = useShield((s) => s.assessment);
  const quote = useShield((s) => s.quote);
  const quoteStatus = useShield((s) => s.quoteStatus);
  const quoteError = useShield((s) => s.quoteError);

  if (!scenario) {
    return (
      <section className="flex min-h-64 flex-1 items-center justify-center rounded-xl border border-dashed border-slate-300 bg-white/60 p-10 text-center">
        <div>
          <p className="text-sm font-semibold text-slate-700">Pick a scenario in the Risk Simulator</p>
          <p className="mt-1 text-xs text-slate-500">
            The storefront renders the selected listing with its live risk evaluation.
          </p>
        </div>
      </section>
    );
  }

  const scenarioMeta = scenario ? scenarioById(scenario) : null;
  const currency = quote?.currency_code ?? "USD";
  const price = quote?.item_price ?? scenarioMeta?.itemPrice ?? "";

  return (
    <section className="flex-1 rounded-xl border border-slate-200 bg-white p-6 shadow-card">
      {quoteStatus === "loading" && (
        <div data-testid="risk-loading" className="animate-pulse space-y-3">
          <div className="h-24 rounded-lg bg-slate-100" />
          <div className="h-4 w-48 rounded bg-slate-100" />
          <div className="h-4 w-72 rounded bg-slate-100" />
        </div>
      )}
      {quoteStatus === "error" && (
        <p data-testid="risk-error" className="rounded-lg bg-red-50 p-3 text-sm text-red-700">
          {quoteError}
        </p>
      )}
      {quoteStatus === "ready" && assessment && (
        <div className="grid gap-6 md:grid-cols-[200px_1fr]">
          <div className="relative overflow-hidden rounded-lg bg-slate-100">
            <img
              src={assessment.pillars.is_ai_generated ? "/product-ai.svg" : "/product-clean.svg"}
              alt="Listing product photo preview"
              className="h-44 w-full object-cover"
            />
            {assessment.pillars.cache_status === "MISS_DEFAULT" && (
              <span className="absolute left-2 top-2 rounded bg-slate-800/80 px-1.5 py-0.5 text-[10px] font-medium text-white">
                conservative defaults
              </span>
            )}
          </div>

          <div>
            <div className="flex flex-wrap items-center gap-3">
              <h1 className="text-lg font-bold text-slate-900">Demo listing: {scenario}</h1>
              <RiskBadge rFinal={assessment.r_final} gate={assessment.gate} />
            </div>

            <dl className="mt-3 grid grid-cols-2 gap-x-8 gap-y-2 md:max-w-md">
              <div>
                <dt className="text-xs text-slate-500">Item price</dt>
                <dd data-testid="item-price" className="text-sm font-semibold text-slate-800">
                  {price ? formatMoney(price, currency) : "—"}
                </dd>
              </div>
              <div>
                <dt className="text-xs text-slate-500">Merchant</dt>
                <dd className="font-mono text-xs text-slate-700">{assessment.merchant_id}</dd>
              </div>
              <div>
                <dt className="text-xs text-slate-500">Trust score</dt>
                <dd className="font-mono text-xs text-slate-700">{assessment.trust_score}</dd>
              </div>
              <div>
                <dt className="text-xs text-slate-500">Closing risk</dt>
                <dd className="font-mono text-xs text-slate-700">{assessment.r_final}</dd>
              </div>
            </dl>

            <div className="mt-4 space-y-1.5 md:max-w-md">
              <PillarRow label="visual" value={assessment.pillars.s_visual} />
              <PillarRow label="semantic" value={assessment.pillars.s_semantic} />
              <PillarRow label="reviews" value={assessment.pillars.s_reviews} />
              <PillarRow label="price" value={assessment.pillars.s_price} />
              <PillarRow label="merchant" value={assessment.s_merchant} />
            </div>

            <div className="mt-4">
              <p className="mb-1 text-xs font-medium uppercase tracking-wide text-slate-400">Reason codes</p>
              <ReasonChips codes={assessment.pillars.reason_codes} />
            </div>
          </div>
        </div>
      )}
    </section>
  );
}
