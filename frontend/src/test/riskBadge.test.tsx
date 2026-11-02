import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { RiskBadge } from "../components/RiskBadge/RiskBadge";
import { bandFor, compareDecimals } from "../components/RiskBadge/bands";

describe("compareDecimals", () => {
  it("compares pure string decimals without numeric parsing", () => {
    expect(compareDecimals("7.80", "25.00")).toBeLessThan(0);
    expect(compareDecimals("25.00", "25.00")).toBe(0);
    expect(compareDecimals("60.01", "60.00")).toBeGreaterThan(0);
    expect(compareDecimals("100.00", "81.00")).toBeGreaterThan(0);
    expect(compareDecimals("0.9381", "0.8500")).toBeGreaterThan(0);
  });
});

describe("bandFor", () => {
  it("maps risk bands", () => {
    expect(bandFor("7.80")).toBe("low");
    expect(bandFor("25.00")).toBe("moderate");
    expect(bandFor("70.25")).toBe("high");
    expect(bandFor("81.00", "BLOCKED_HIGH_RISK")).toBe("blocked");
  });

  it("rejects at the coverage gate", () => {
    expect(bandFor("81.00")).toBe("blocked");
    expect(bandFor("80.00")).toBe("high");
  });
});

describe("RiskBadge", () => {
  it("renders band text for the closing risk", () => {
    render(<RiskBadge rFinal="7.80" />);
    expect(screen.getByTestId("risk-badge")).toHaveTextContent("Low risk");
  });

  it("renders blocked styling above the gate", () => {
    render(<RiskBadge rFinal="81.00" gate="BLOCKED_HIGH_RISK" />);
    expect(screen.getByTestId("risk-badge")).toHaveTextContent("Blocked");
  });
});
