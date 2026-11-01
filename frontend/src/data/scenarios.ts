export type ScenarioId = "trusted" | "scam" | "offender";

export interface ScenarioMeta {
  id: ScenarioId;
  label: string;
  merchantId: string;
  blurb: string;
  accent: "emerald" | "red" | "orange";
  icon: string;
}

export const scenarioList: ScenarioMeta[] = [
  {
    id: "trusted",
    label: "Trusted merchant",
    merchantId: "mer_audio_den",
    blurb: "Established store with clean history. Low risk, cheap fee.",
    accent: "emerald",
    icon: "shield-check",
  },
  {
    id: "scam",
    label: "AI-generated scam listing",
    merchantId: "mer_dash_deals",
    blurb: "Brand-new store with a synthetic photo and extreme underpricing.",
    accent: "red",
    icon: "alert-triangle",
  },
  {
    id: "offender",
    label: "Repeat offender",
    merchantId: "mer_refurb_find",
    blurb: "Verified fraud history pushes the closing price beyond the safety gate.",
    accent: "orange",
    icon: "gavel",
  },
];

export const scenarioById = (id: ScenarioId): ScenarioMeta =>
  scenarioList.find((s) => s.id === id)!;
