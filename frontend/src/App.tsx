import { useCallback, useEffect, useState } from "react";

import { SystemPanel } from "./components/SystemPanel";
import { ApiError, fetchSystemStatus } from "./lib/api";
import type { SystemStatus } from "./lib/types";

type LoadState =
  | { kind: "loading" }
  | { kind: "ready"; status: SystemStatus }
  | { kind: "error"; message: string };

export function App() {
  const [state, setState] = useState<LoadState>({ kind: "loading" });

  const load = useCallback(async () => {
    setState({ kind: "loading" });
    try {
      setState({ kind: "ready", status: await fetchSystemStatus() });
    } catch (error) {
      const message =
        error instanceof ApiError ? error.message : "unexpected error loading system status";
      setState({ kind: "error", message });
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  return (
    <div className="app">
      <header className="topbar">
        <span className="topbar__brand">ARBITRAGE TERMINAL</span>
        <span className="topbar__tag">
          {state.kind === "ready" ? state.status.profile.toUpperCase() : "—"}
        </span>
        <span className="topbar__spacer" />
        <button type="button" className="topbar__action" onClick={() => void load()}>
          Refresh
        </button>
      </header>

      <main className="content">
        {state.kind === "loading" && <p className="notice">Loading system status…</p>}

        {state.kind === "error" && (
          <div className="notice notice--error" role="alert">
            <p>{state.message}</p>
            <p className="notice__hint">
              Start the backend with <code>uv run trading-bot-api</code> from{" "}
              <code>backend/</code>.
            </p>
          </div>
        )}

        {state.kind === "ready" && <SystemPanel status={state.status} />}
      </main>

      <footer className="footer">
        Phase 0 shell — the full terminal UI is built in Phase 12 on real data.
      </footer>
    </div>
  );
}
