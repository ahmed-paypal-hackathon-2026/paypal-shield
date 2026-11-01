import { useShield } from "../../state/store";
import { scenarioList, type ScenarioId, type ScenarioMeta } from "../../data/scenarios";

const accentClasses: Record<ScenarioMeta["accent"], string> = {
  emerald: "hover:border-emerald-400 focus-visible:border-emerald-400",
  red: "hover:border-red-400 focus-visible:border-red-400",
  orange: "hover:border-orange-400 focus-visible:border-orange-400",
};

const activeClasses: Record<ScenarioMeta["accent"], string> = {
  emerald: "border-emerald-500 bg-emerald-50/60 ring-1 ring-emerald-500",
  red: "border-red-500 bg-red-50/60 ring-1 ring-red-500",
  orange: "border-orange-500 bg-orange-50/60 ring-1 ring-orange-500",
};

const iconPaths: Record<string, string> = {
  "shield-check": "M9 12.75 11.25 15 15 9.75M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0Z",
  "alert-triangle": "M12 9v3.75m9-3.75-9 15-9-15 9-15 9 15Z",
  gavel: "M12.75 19.5h-.75m-9 0h9m-9 0a3 3 0 0 1-3-3V9a3 3 0 0 1 3-3h9a3 3 0 0 1 3 3v7.5a3 3 0 0 1-3 3Z",
};

function ScenarioCard({ id, active, onPick }: { id: ScenarioId; active: boolean; onPick: (id: ScenarioId) => void }) {
  const meta = scenarioList.find((s) => s.id === id)!;
  return (
    <button
      type="button"
      data-testid={`scenario-${id}`}
      aria-pressed={active}
      onClick={() => onPick(id)}
      className={`w-full rounded-xl border p-4 text-left transition-colors ${
        active ? activeClasses[meta.accent] : `border-slate-200 bg-white ${accentClasses[meta.accent]}`
      }`}
    >
      <div className="flex items-center gap-2">
        <svg
          xmlns="http://www.w3.org/2000/svg"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="1.5"
          className={`h-5 w-5 ${
            meta.accent === "emerald" ? "text-emerald-600" : meta.accent === "red" ? "text-red-500" : "text-orange-500"
          }`}
        >
          <path strokeLinecap="round" strokeLinejoin="round" d={iconPaths[meta.icon]} />
        </svg>
        <span className="text-sm font-semibold text-slate-800">{meta.label}</span>
      </div>
      <p className="mt-1 text-xs leading-5 text-slate-500">{meta.blurb}</p>
    </button>
  );
}

export function SimulatorDrawer() {
  const scenario = useShield((s) => s.scenario);
  const setScenario = useShield((s) => s.setScenario);
  const quoteStatus = useShield((s) => s.quoteStatus);
  return (
    <aside className="w-full max-w-sm shrink-0 space-y-3">
      <div className="rounded-xl border border-slate-200 bg-white p-4 shadow-card">
        <div className="flex items-center justify-between">
          <h2 className="text-sm font-bold text-slate-800">Risk Simulator</h2>
          <span className="text-[11px] font-medium uppercase tracking-wide text-slate-400">
            Demo scenarios
          </span>
        </div>
        <p className="mt-1 text-xs text-slate-500">
          One click swaps the storefront between three merchants with different risk profiles.
        </p>
        <div className="mt-3 space-y-2">
          {scenarioList.map((s) => (
            <ScenarioCard key={s.id} id={s.id} active={scenario === s.id} onPick={(id) => void setScenario(id)} />
          ))}
        </div>
        {quoteStatus === "loading" && (
          <p data-testid="simulator-loading" className="mt-3 animate-pulse text-xs text-slate-500">
            Evaluating risk pillars...
          </p>
        )}
      </div>
    </aside>
  );
}
