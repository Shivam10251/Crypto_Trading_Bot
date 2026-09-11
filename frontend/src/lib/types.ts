// Mirrors backend/src/trading_bot/api/schemas.py.
// Phase 13 will generate these from the OpenAPI schema; for now they are
// maintained by hand and asserted against the live backend in its tests.

export type ComponentStatus = "HEALTHY" | "DEGRADED" | "OFFLINE";

export interface ComponentHealth {
  name: string;
  status: ComponentStatus;
  detail: string;
}

export interface SystemStatus {
  profile: string;
  version: string;
  execution_mode: string;
  live_execution_armed: boolean;
  exchange: string;
  // null when the backend cannot read which markets are monitored.
  monitored_spot_markets: number | null;
  monitored_perpetual_markets: number | null;
  components: ComponentHealth[];
  server_time: string;
}
