import "@testing-library/jest-dom/vitest";
import { vi, afterEach } from "vitest";
import { cleanup } from "@testing-library/react";
import { useShield } from "../state/store";
import { sharedMock } from "../api/mock";

afterEach(() => {
  cleanup();
});

Object.defineProperty(window, "matchMedia", {
  writable: true,
  value: (query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: vi.fn(),
    removeListener: vi.fn(),
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    dispatchEvent: vi.fn(),
  }),
});

afterEach(async () => {
  sharedMock.stop();
  useShield.setState({
    api: null,
    mode: "connecting",
    health: null,
    scenario: null,
    assessment: null,
    quote: null,
    quoteStatus: "idle",
    quoteError: "",
    optIn: true,
    checkoutPhase: "idle",
    order: null,
    capture: null,
    checkoutError: "",
    claims: [],
    claimStatus: "idle",
    claimError: "",
    selectedClaimId: null,
    transactions: [],
    merchants: [],
    summary: null,
  });
});
