const BAND_LOW = "25.00";
const BAND_HIGH = "60.00";
const GATE = "80.00";

/** Lexicographic comparison for canonical non-negative decimal strings. No floats. */
export function compareDecimals(a: string, b: string): number {
  const [ai = "", af = ""] = a.split(".");
  const [bi = "", bf = ""] = b.split(".");
  if (ai.length !== bi.length) return ai.length > bi.length ? 1 : -1;
  if (ai !== bi) return ai > bi ? 1 : -1;
  const afp = af.padEnd(10, "0");
  const bfp = bf.padEnd(10, "0");
  return afp === bfp ? 0 : afp > bfp ? 1 : -1;
}

export type RiskBand = "low" | "moderate" | "high" | "blocked";

export interface BandStyle {
  label: string;
  chipClass: string;
  dotClass: string;
}

const styles: Record<RiskBand, BandStyle> = {
  low: {
    label: "Low risk",
    chipClass: "bg-emerald-50 text-emerald-700 ring-emerald-600/20",
    dotClass: "bg-emerald-500",
  },
  moderate: {
    label: "Moderate risk",
    chipClass: "bg-amber-50 text-amber-700 ring-amber-600/20",
    dotClass: "bg-amber-500",
  },
  high: {
    label: "High risk",
    chipClass: "bg-orange-50 text-orange-700 ring-orange-600/20",
    dotClass: "bg-orange-500",
  },
  blocked: {
    label: "Blocked",
    chipClass: "bg-red-50 text-red-700 ring-red-600/20",
    dotClass: "bg-red-500",
  },
};

export function bandFor(rFinal: string, gate: string = "COVERAGE_AVAILABLE"): RiskBand {
  if (gate === "BLOCKED_HIGH_RISK") return "blocked";
  if (compareDecimals(rFinal, BAND_LOW) < 0) return "low";
  if (compareDecimals(rFinal, BAND_HIGH) <= 0) return "moderate";
  if (compareDecimals(rFinal, GATE) <= 0) return "high";
  return "blocked";
}

export function bandStyle(band: RiskBand): BandStyle {
  return styles[band];
}
