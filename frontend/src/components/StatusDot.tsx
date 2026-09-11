import type { ComponentStatus } from "../lib/types";

const LABEL: Record<ComponentStatus, string> = {
  HEALTHY: "healthy",
  DEGRADED: "degraded",
  OFFLINE: "offline",
};

/** Small glowing indicator; colour encodes subsystem health. */
export function StatusDot({ status }: { status: ComponentStatus }) {
  return (
    <span
      className={`status-dot status-dot--${status.toLowerCase()}`}
      role="img"
      aria-label={LABEL[status]}
      title={LABEL[status]}
    />
  );
}
