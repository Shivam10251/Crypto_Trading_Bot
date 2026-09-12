# Architecture

## Principles

1. **The strategy is the centre, and it is isolated.** Strategy code consumes
   normalized market data and emits signals. It never imports the database,
   the Binance client, FastAPI, or anything about the dashboard.
2. **One implementation per concern.** The same strategy object runs in
   backtest, paper and live modes. Swapping the *execution adapter* changes the
   mode, never the strategy.
3. **Boundaries are interfaces.** Exchanges, execution venues and storage sit
   behind narrow interfaces so a second exchange does not touch strategy code.
4. **Fail safe.** Anything abnormal - stale data, latency spikes, API errors -
   stops trading rather than guessing.
5. **Store everything decision-relevant.** Rejected opportunities are research
   data; discarding them destroys the ability to evaluate the strategy.

## Pipeline

```
Binance WebSocket / REST        (Phase 3)
            │
            ▼
   ExchangeAdapter              (Phase 2)  normalize, validate, timestamp
            │
            ▼
   Market Data Engine           (Phase 3)  order books, staleness, latency
            │
            ▼
   Market Monitor               (Phase 4)  spreads, imbalance, liquidity
            │
            ▼
   Strategy  (spot vs perp)     (Phase 5)  detect → calculate edge → validate
            │
            ▼
   Transaction Cost Model       (Phase 6)  fee schedule, both-way slippage,
            │                              funding at settlements crossed
            │                              NET EDGE = gross − all costs
            ▼
   Opportunity Engine           (Phase 7)  every opportunity, as an episode
            │
            ▼
   Risk Engine                  (Phase 9)  APPROVED / REJECTED / PAUSED,
            │                              reserving atomically against the
            │                              same account Phase 8 built
            ▼
   Execution Adapter            (Phase 8)  Paper today, Live behind a flag (17)
            │
            ▼
   Portfolio & P&L              (Phase 10) exits, equity, drawdown
            │
            ▼
   API + WebSocket  →  Dashboard (Phases 12-13)
```

Phase 8's paper account gate - cash, inventory, margin, exposure - is no
longer a separate stage between the opportunity engine and the adapter.
Phase 9 sits in front of the adapter and reserves against that same account
atomically, so there is exactly one place that accounts for paper exposure
rather than two that could disagree. On top of that reservation it adds what
Phase 8 could not: a durable `RiskDecision` (`APPROVED`/`REJECTED`/`PAUSED`)
written to `risk_events` *before* an order may be submitted, independent
data-quality and timing checks the strategy's own thresholds do not gate on,
a durable kill switch, and a post-trade review of what execution actually
did. See [risk-management.md](risk-management.md) for the full account.

## Backend layout

| Module | Responsibility | Depends on |
| --- | --- | --- |
| `trading_bot.core.config` | Layered settings, live-trading guards | nothing |
| `trading_bot.core.logging` | Structured logs, credential redaction | config |
| `trading_bot.db` | Engine, sessions, ORM models, retention | config, logging |
| `trading_bot.exchange` | Venue boundary: adapter interface + normalized models | config, logging |
| `trading_bot.exchange.streaming` | Streaming boundary: stream endpoints + parser contract | exchange |
| `trading_bot.exchange.binance` | binance.com spot + USDⓈ-M market data and streams | exchange, config |
| `trading_bot.marketdata` | Live engine: connections, local books, staleness, snapshots, recorder | exchange, config, db |
| `trading_bot.monitoring` | Market selection, per-market statistics, terminal view | marketdata, exchange, config, strategy |
| `trading_bot.strategy` | Strategy contract, domain types, cost model, basis strategy, runner | exchange + marketdata models only |
| `trading_bot.opportunities` | Episode tracking and the research record | strategy, db |
| `trading_bot.execution` | Bounded dispatcher, account reservations, adapter contract, paper simulator, leg coordination, order/fill/position record | strategy + marketdata models, db |
| `trading_bot.api` | HTTP contract for the dashboard | config, db |
| `trading_bot.main` | Composition root: wires everything | all of the above |

Dependencies point inward. `core` imports nothing from the project, and
`strategy` imports only normalized domain types - no database, no adapter, no
FastAPI - which is what keeps it testable without infrastructure and runnable
unchanged in backtest, paper and live modes. `monitoring` depends on `strategy`
only to draw it, never the other way round.

## Communication

```
Browser ──HTTP──▶ Vite dev server (5173) ──proxy /api──▶ FastAPI (8000) ──asyncpg──▶ PostgreSQL (5432)
        ◀─WS───────────────────── (Phase 13) ─────────────┘
```

- In development the Vite proxy forwards `/api` to the backend, so the browser
  sees a single origin. In production the built assets are served separately
  and CORS origins are configured explicitly.
- All endpoints live under `/api/v1`. Types in `frontend/src/lib/types.ts`
  mirror the Pydantic schemas; Phase 13 generates them from OpenAPI instead.
- Phase 13 adds WebSocket streams so the dashboard does not poll the database.

## Configuration and process model

Settings resolve from `config/base.yaml`, then `config/<profile>.yaml`, then
`TB_*` environment variables. The resolved object is frozen: risk limits and
execution flags cannot change under a running process.

Two processes run today, sharing configuration and the database: the API and
the market-data service (`make market-data`), which also hosts the strategy
runner and the opportunity recorder. The API cannot ask the service how it is
doing, so it judges it by its output - which markets the service last selected
(`markets.is_monitored`), whether each one's newest stored quote is recent, and
how recently an opportunity was recorded - rather than by assumption.

## Status

Built: configuration, logging, database layer with the full 13-table data
model and migrations, retention, the exchange abstraction with a Binance
market-data adapter (spot + USDⓈ-M), the real-time market-data engine and its
service, configurable market selection and per-market monitoring, the strategy
framework with the spot/perpetual basis strategy, the transaction cost model,
the opportunity engine recording every detection to PostgreSQL with the
evidence behind it, the paper execution engine and its order/fill record, the
risk engine gating every order behind a durable decision and a kill switch,
API skeleton, health and system-status endpoints, frontend shell, test
tooling.

Not built: exits/P&L portfolio service, dashboard. Daily-loss and
consecutive-loss limits exist as configuration and policy but are honestly
reported as deferred - they need Phase 10's realised P&L to mean anything.
The system-status endpoint reports the not-yet-built subsystems as `OFFLINE`
with the phase that will implement them.

### The exchange boundary

Strategies depend on `ExchangeAdapter` and the normalized models, never on
Binance specifics, so a second venue means writing one adapter. Market-data
methods are abstract because every venue must provide them; optional
capabilities and execution methods raise by default rather than returning an
invented answer. Payload validation happens once, in the venue's mapping layer:
past that point the data is trusted, and anything malformed - crossed book,
zero price, missing field - raises `ExchangeDataError` instead of reaching a
strategy.

### The streaming boundary

Live data splits the same way. A venue implements `MarketStreamSource`: which
WebSocket URLs carry which markets, and how to parse one message into a
normalized `Quote`, `DepthDiff` or `TickerStats`. Everything else lives once, in
`trading_bot.marketdata`, for every venue:

```
StreamConnection (reconnect, heartbeat, idle timeout)
        │ raw message + local receipt time
        ▼
MarketStreamSource.parse  (venue-specific, validating)
        │ Quote / DepthDiff / TickerStats
        ▼
MarketDataEngine  ── LocalOrderBook per market (snapshot + diffs, gap → rebuild)
        │           watchdog (STALE after stale_after_ms)
        ▼
MarketSnapshot  ──▶ snapshot() / updates()   (strategies, monitor, risk)
                ──▶ MarketDataRecorder       (market_data, system_events)
```

A market's book is either `SYNCED` or not published at all; there is no state
in which a consumer receives depth the engine cannot vouch for.

### Monitoring

`trading_bot.monitoring` sits on the engine's read API. `select_universe`
decides what the engine streams (configuration: top N pairs by weaker-leg 24h
volume, or explicit lists); `MarketMonitor` samples every snapshot once a
second into `MarketMetrics` with rolling means and latency percentiles.
Sampling keeps the cost flat as markets are added: a hundred markets cost a
hundred snapshot reads per interval however busy they are.

Staleness has two scopes. A connection silent for `stale_after_ms` (2 s)
makes every market on it stale; a single market is stale on its own only after
`market_silence_ms` (30 s), because the venue pushes only changes and a quiet
market is unchanged rather than stale.

### The strategy boundary

`trading_bot.strategy` sits on the monitor's output and reaches nothing else.
A strategy is handed a `MarketView` per market - snapshot, metrics, spec and,
for perpetuals, funding - and returns opportunities, edges and signals. It has
no adapter, no session and no settings object, so the same instance runs under
backtest, paper and live execution without change.

```
MarketSnapshot + MarketMetrics + FundingInfo
        │
        ▼
StrategyRunner ── per strategy ──▶ on_market_data
        │                            detect_opportunities
        │                            calculate_edge      (CostModel)
        │                            generate_signal
        │                            validate_signal
        ▼
EvaluatedOpportunity  - the signal, or the reason there is none
```

Every opportunity is priced and kept, including the rejected ones: they are the
research dataset, and a list of zero opportunities must be distinguishable from
a broken feed. `CostModel` is an interface, and Phase 6 replaced the implementation behind it
without changing it. Fees come from the published schedule per instrument class
and side of the book; slippage is walked in both directions against the real
depth; funding is charged per settlement actually crossed, not accrued
continuously. What cannot be estimated is refused rather than guessed.

### The research record

`trading_bot.opportunities` turns evaluations into stored rows. The unit is an
**episode** - one contiguous period during which the same strategy sees the
same discrepancy in the same direction - not one row per evaluation: measured
live, per-cycle rows would be 4.0M a day against 81K for episodes, almost all
of them restating the previous second.

```
StrategyEvaluation (every second)
        │
        ▼
EpisodeTracker   opens / absorbs / closes, keeping the BEST moment
        │ closed episodes
        ▼
OpportunityRecorder ──▶ opportunities  (status + every gate it failed)
                    ──▶ signals        (one row per leg)
```

### The execution boundary

`trading_bot.execution` is where `trading_bot.risk` (Phase 9) sits in front
of the adapter. A strategy's validated signal becomes two `OrderRequest`s -
one per leg, never a bundle - because that is the only shape in which leg
risk is expressible: one can fill while the other does not.

```
Signal ──▶ bounded dispatcher ──▶ RiskEngine.evaluate ──▶ APPROVED ──▶ ExecutionCoordinator
                                        │                                  │ both legs at once
                                        ▼                                  ▼
                              REJECTED / PAUSED               ExecutionAdapter ──▶ ExecutionResult
                              durable risk_events row,         PaperExecutionAdapter (Phase 8)
                              no order                         LiveExecutionAdapter  (Phase 17, off)
                                                                    │
                                                                    ▼
                                                          ExecutionAttempt  - hedged, naked by N, or nothing filled
                                                                    │
                                        ┌───────────────────────────┴───────────────────────────┐
                                        ▼                                                        ▼
                          RiskEngine.evaluate_post_trade                              ExecutionRecorder
                          (naked exposure, slippage, latency,                         ──▶ orders + fills + open
                          skew, actual exposure limits)                                   positions, orders.risk_event_id
```

`RiskEngine.evaluate` reserves against the same `PaperAccount` Phase 8 built
- order, position and gross-exposure limits, cash, spot inventory, perpetual
margin and borrow capacity all come from that one reservation, checked
atomically against a durable kill-switch guard so a switch flip cannot race
a concurrent approval. Only once that reservation succeeds *and* the
`APPROVED` decision is durably written to `risk_events` does the dispatcher
call the coordinator; a `REJECTED` or `PAUSED` verdict never reaches it, so
no order row is ever created for a risk refusal.

Approval is not the last word. `RiskEngine.admit` runs inside the
coordinator, in the instant before the adapter is handed anything, and
re-checks everything that can change in the meantime - the kill switch,
signal expiry, and every staleness and latency limit, recomputed from
timestamps against the current clock. A refusal there releases the
reservation and produces no order. Shadow probes reserve against a second,
isolated `PaperAccount`, so research can neither consume nor be blocked by
trading capacity. Aborted reservations remain retryable and must pass every
limit again; after a real settlement, actual notionals are checked for adverse
fill-price breaches. See [risk-management.md](risk-management.md) for the full
set of checks, the kill switch's precise guarantees, and what is honestly
deferred to Phase 10.

The simulator reads the book **at fill time**, after the configured latency
has elapsed, so the market moves before the order lands. Nothing in it is
random: rejections come from venue filters and account constraints, while
partial fills come from observed depth. IOC/FOK are evaluated once at arrival;
GTC is refused until trades and queue position can support it. There is no
fill-probability knob, because a simulator whose disappointments are drawn
from a seed measures the seed.

An episode that could never be priced is stored as `UNPRICEABLE` with no costs
on it: it happened, so it is counted, and an invented zero would corrupt every
query asking what survived costs. Each row carries the quotes, books, fills,
filters, rates and assumptions behind it, because the raw tables it came from
are purged in days and opportunities never are. Opportunities and signals are
never purged - unlike raw market data, they are the point of the exercise.
