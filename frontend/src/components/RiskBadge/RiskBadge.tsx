import { bandFor, bandStyle } from "./bands";

export function RiskBadge({
  rFinal,
  gate = "COVERAGE_AVAILABLE",
  size = "md",
}: {
  rFinal: string;
  gate?: string;
  size?: "sm" | "md";
}) {
  const band = bandFor(rFinal, gate);
  const style = bandStyle(band);
  const pad = size === "sm" ? "px-2 py-0.5 text-[11px]" : "px-2.5 py-1 text-xs";  return (
    <span
      data-testid="risk-badge"
      className={`inline-flex items-center gap-1.5 rounded-full font-medium ring-1 ring-inset ${pad} ${style.chipClass}`}
    >
      <span className={`h-1.5 w-1.5 rounded-full ${style.dotClass}`} aria-hidden />
      {style.label}
    </span>
  );
}

export function ReasonChips({ codes }: { codes: string[] }) {
  if (codes.length === 0) {
    return <span className="text-xs text-slate-400">No flags</span>;
  }
  return (
    <div className="flex flex-wrap gap-1.5">
      {codes.map((code) => (
        <span
          key={code}
          title={code}
          className="inline-flex items-center rounded-md bg-slate-100 px-2 py-0.5 text-[11px] font-medium text-slate-600"
        >
          {code}
        </span>
      ))}
    </div>
  );
}
