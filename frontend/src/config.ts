const bool = (v: string | undefined, fallback: boolean): boolean =>
  v === undefined ? fallback : v.toLowerCase() === "true";

export const appConfig = {
  apiBaseUrl: import.meta.env.VITE_API_BASE_URL ?? "http://localhost:8000",
  paypalClientId: import.meta.env.VITE_PAYPAL_CLIENT_ID,
  allowMock: bool(import.meta.env.VITE_ALLOW_MOCK, false),
  currencySymbol: "USD",
  claimReviewerToken: import.meta.env.VITE_CLAIM_REVIEWER_TOKEN ?? "shield-reviewer-secret-key",
} as const;
