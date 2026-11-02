import { describe, it, expect, beforeEach, vi } from "vitest";
import { render, screen, act } from "@testing-library/react";
import { userEvent } from "@testing-library/user-event";
import { App } from "../App";
import { useShield } from "../state/store";
import { sharedMock } from "../api/mock";

beforeEach(async () => {
  useShield.setState({
    api: sharedMock,
    mode: "mock",
    health: { status: "ok", paypal_sandbox_connected: false, demo_mode: true },
  });
});

describe("simulator scenarios (REQ-FE-01)", () => {
  it("shows the trusted fee and fee line in one click", async () => {
    render(<App />);
    const user = userEvent.setup();
    await user.click(screen.getByTestId("scenario-trusted"));
    await vi.waitFor(() => expect(screen.getByTestId("item-price")).toHaveTextContent("$120.00"));
    expect(screen.getByTestId("risk-badge")).toHaveTextContent("Low risk");
    expect(screen.getByTestId("opt-in-row")).toHaveTextContent("$0.47");
  });

  it("flags the AI-Generated Scam listing with reason codes", async () => {
    render(<App />);
    const user = userEvent.setup();
    await user.click(screen.getByTestId("scenario-scam"));
    await vi.waitFor(() => expect(screen.getByTestId("risk-badge")).toHaveTextContent("High risk"));
    expect(screen.getByText("VIS_AI_GENERATED")).toBeInTheDocument();
    expect(screen.getByTestId("opt-in-row")).toHaveTextContent("2.81");
  });

  it("blocks the Repeat-Offender checkout with a prominent warning", async () => {
    render(<App />);
    const user = userEvent.setup();
    await user.click(screen.getByTestId("scenario-offender"));
    await vi.waitFor(() => expect(screen.getByTestId("blocked-panel")).toBeInTheDocument());
    expect(screen.getByTestId("blocked-panel")).toHaveTextContent("without a Protection Plan");
    expect(screen.queryByTestId("opt-in-row")).not.toBeInTheDocument();
  });
});

describe("paypal checkout via mock flow (REQ-FE-02)", () => {
  it("runs create → approve → capture end to end", async () => {
    render(<App />);
    const user = userEvent.setup();
    await user.click(screen.getByTestId("scenario-trusted"));
    await vi.waitFor(() => expect(screen.getByTestId("checkout-button")).toBeInTheDocument());
    await user.click(screen.getByTestId("checkout-button"));
    await vi.waitFor(() => expect(screen.getByTestId("capture-result")).toBeInTheDocument(), { timeout: 5000 });
    expect(screen.getByTestId("capture-result")).toHaveTextContent("Paid");
    expect(screen.getByTestId("capture-result")).toHaveTextContent("120.47");
  });
});

describe("ledger live rows (REQ-FE-06)", () => {
  it("a stream transaction event appears without reload", async () => {
    render(<App />);
    const user = userEvent.setup();
    await user.click(screen.getByTestId("scenario-trusted"));
    await user.click(screen.getByTestId("tab-ledger"));
    expect(screen.getByTestId("ledger-grid")).toBeInTheDocument();
    act(() => {
      useShield.getState().acceptStreamEvent({
        topic: "transaction",
        action: "add",
        record: {
          internal_order_id: "ord_stream_1",
          merchant_id: "mer_audio_den",
          item_price: "33.00",
          protection_fee: "0.83",
          r_final: "7.80",
          state: "CAPTURED",
          created_at: new Date().toISOString(),
        },
      });
    });
    expect(screen.getByText("ord_stream_1")).toBeInTheDocument();
  });
});
