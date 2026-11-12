export type ScenarioId = "trusted" | "scam" | "offender";

export interface ScenarioMeta {
  id: ScenarioId;
  label: string;
  itemTitle: string;
  itemPrice: string;
  merchantId: string;
  itemHash: string;
  blurb: string;
  accent: "emerald" | "red" | "orange";
  icon: string;
}

export const scenarioList: ScenarioMeta[] = [
  {
    id: "trusted",
    label: "Trusted merchant",
    itemTitle: "AudioMax Studio Headphones",
    itemPrice: "120.00",
    merchantId: "mer_audio_den",
    itemHash: "b3a9e55dde37b80d97fa8713a01dda91d3c1c7f2e33bd0cf8a35f4f4e3a11c05",
    blurb: "Established store with clean history. Low risk, cheap fee.",
    accent: "emerald",
    icon: "shield-check",
  },
  {
    id: "scam",
    label: "AI-generated scam listing",
    itemTitle: "AuraGlow 8K Camera Bundle",
    itemPrice: "80.00",
    merchantId: "mer_dash_deals",
    itemHash: "9d40b7a2c02df11bff6f398ba9dfde04c8f2fba666561e4b7a4d9b4c4f00ad21",
    blurb: "Brand-new store with a synthetic photo and extreme underpricing.",
    accent: "red",
    icon: "alert-triangle",
  },
  {
    id: "offender",
    label: "Repeat offender",
    itemTitle: "RefurbOne GPU, like new",
    itemPrice: "60.00",
    merchantId: "mer_refurb_find",
    itemHash: "4c8d2ca1d5d63a1eb9b60a15b4a1f8bff38dcdbf7a8c3d10b8dcfc8d3a417bae",
    blurb: "Verified fraud history pushes the closing price beyond the safety gate.",
    accent: "orange",
    icon: "gavel",
  },
];

export const scenarioById = (id: ScenarioId): ScenarioMeta =>
  scenarioList.find((s) => s.id === id)!;
