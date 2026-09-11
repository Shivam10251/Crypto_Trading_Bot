# Risk Management

Status: **not implemented.** Phase 9 builds the risk engine. The configuration
limits and live-trading guards exist and are tested today.

## Position in the pipeline

The risk engine sits between strategy and execution. A signal cannot reach an
execution adapter without a `RiskDecision`:

```
Signal → Risk Engine → APPROVED  → Execution
                     → REJECTED  → logged, no order
                     → PAUSED    → strategy disabled, logged
```

It is independent of the strategy on purpose: a buggy or over-confident
strategy must not be able to talk its way past limits.

## Configured limits

All in `config/base.yaml`, overridable per profile and validated at startup
(`RiskConfig` rejects inconsistent hierarchies, e.g. an order limit larger than
the position limit):

| Limit | Default | Guards against |
| --- | --- | --- |
| `max_order_notional_usd` | 1,000 | Fat-finger and runaway sizing |
| `max_position_notional_usd` | 5,000 | Concentration in one instrument |
| `max_total_exposure_usd` | 10,000 | Portfolio-wide leverage |
| `max_daily_loss_usd` | 200 | Losing day compounding |
| `max_consecutive_losses` | 5 | A strategy that has stopped working |
| `max_slippage_bps` | 15 | Executing into thin books |
| `max_latency_ms` | 500 | Acting on information that has aged out |
| `max_stale_data_ms` | 2,000 | Trading on a frozen feed |

## Intended responses

| Condition | Response |
| --- | --- |
| Market data older than `max_stale_data_ms` | Reject trade |
| Exposure limit reached | Reject trade |
| Daily loss limit reached | Disable strategy for the day |
| Consecutive-loss limit reached | Pause strategy, require review |
| Slippage or latency above limit | Pause strategy, log for analysis |
| Exchange or database failure | Fail safe: stop trading |

Every rejection, pause and kill-switch trigger is written to `risk_events` with
the inputs that caused it. A decision that cannot be explained after the fact is
a bug.

## Kill switch

A single control that halts trading immediately: stop accepting signals, cancel
working orders, and refuse new ones until explicitly re-armed. Exposed to the
dashboard (Phase 12) and callable from the backend. It is a real control path,
not a UI affordance.

## Data-quality gates

Bad data is treated as a risk event, not an edge case:

- non-positive or absurd prices, crossed books (bid ≥ ask)
- missing or non-monotonic sequence numbers
- feed gaps and reconnects
- exchange timestamps far from local time

## Safety properties already enforced

- Live execution is impossible outside an explicitly armed production process.
- Settings are frozen after resolution: limits cannot drift at runtime.
- Credential-shaped log fields are redacted centrally.
- The API starts even when the database is down, and reports `503` from
  `/health/ready` — monitoring stays reachable during an outage.
