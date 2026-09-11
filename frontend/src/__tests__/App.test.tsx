import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { App } from "../App";
import type { SystemStatus } from "../lib/types";

const STATUS: SystemStatus = {
  profile: "development",
  version: "0.1.0",
  execution_mode: "paper",
  live_execution_armed: false,
  exchange: "binance",
  monitored_spot_markets: 1,
  monitored_perpetual_markets: 1,
  components: [
    { name: "API", status: "HEALTHY", detail: "serving requests" },
    { name: "Market Data", status: "OFFLINE", detail: "not implemented until Phase 3" },
  ],
  server_time: "2026-09-11T06:00:00Z",
};

afterEach(() => {
  vi.unstubAllGlobals();
});

function stubStatus(payload: unknown, status = 200) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => new Response(JSON.stringify(payload), { status })),
  );
}

describe("App", () => {
  it("renders the backend-reported configuration", async () => {
    stubStatus(STATUS);
    render(<App />);

    expect(await screen.findByText("System Health")).toBeInTheDocument();
    expect(screen.getByText("development")).toBeInTheDocument();
    expect(screen.getByText("PAPER")).toBeInTheDocument();
    expect(screen.getByText("BINANCE")).toBeInTheDocument();
    expect(screen.getByText("1 spot / 1 perp")).toBeInTheDocument();
  });

  it("shows live trading as disabled unless the backend says otherwise", async () => {
    stubStatus(STATUS);
    render(<App />);

    expect(await screen.findByText("DISABLED")).toBeInTheDocument();
    expect(screen.queryByText("ARMED")).not.toBeInTheDocument();
  });

  it("reports unbuilt subsystems as offline instead of hiding them", async () => {
    stubStatus(STATUS);
    render(<App />);

    expect(await screen.findByText("Market Data")).toBeInTheDocument();
    expect(screen.getByText("not implemented until Phase 3")).toBeInTheDocument();
    expect(screen.getAllByRole("img", { name: "offline" })).toHaveLength(1);
    expect(screen.getAllByRole("img", { name: "healthy" })).toHaveLength(1);
  });

  it("explains how to start the backend when it is unreachable", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        throw new TypeError("Failed to fetch");
      }),
    );
    render(<App />);

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/cannot reach backend/);
    expect(alert).toHaveTextContent(/uv run trading-bot-api/);
  });

  it("surfaces a backend error status", async () => {
    stubStatus({}, 503);
    render(<App />);

    await waitFor(() =>
      expect(screen.getByRole("alert")).toHaveTextContent(/HTTP 503/),
    );
  });
});
