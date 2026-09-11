import type { SystemStatus } from "../lib/types";
import { StatusDot } from "./StatusDot";

/**
 * Phase 0 shell: renders exactly what the backend reports, including the
 * subsystems that are still OFFLINE. No placeholder metrics are invented -
 * the real dashboard arrives in Phase 12 on top of real data.
 */
export function SystemPanel({ status }: { status: SystemStatus }) {
  return (
    <section className="panel" aria-labelledby="system-heading">
      <header className="panel__header">
        <h2 id="system-heading">System Health</h2>
        <span className="panel__meta">backend v{status.version}</span>
      </header>

      <dl className="kv">
        <div className="kv__row">
          <dt>Profile</dt>
          <dd>{status.profile}</dd>
        </div>
        <div className="kv__row">
          <dt>Execution</dt>
          <dd>{status.execution_mode.toUpperCase()}</dd>
        </div>
        <div className="kv__row">
          <dt>Live trading</dt>
          <dd>{status.live_execution_armed ? "ARMED" : "DISABLED"}</dd>
        </div>
        <div className="kv__row">
          <dt>Exchange</dt>
          <dd>{status.exchange.toUpperCase()}</dd>
        </div>
        <div className="kv__row">
          <dt>Markets</dt>
          <dd>
            {status.monitored_spot_markets} spot / {status.monitored_perpetual_markets} perp
          </dd>
        </div>
      </dl>

      <ul className="components">
        {status.components.map((component) => (
          <li key={component.name} className="components__item">
            <StatusDot status={component.status} />
            <span className="components__name">{component.name}</span>
            <span className="components__detail">{component.detail}</span>
          </li>
        ))}
      </ul>
    </section>
  );
}
