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
   Transaction Cost Model       (Phase 6)  fees, slippage, funding, buffer
            │                              NET EDGE = gross − all costs
            ▼
   Opportunity Engine           (Phase 7)  persist every opportunity + status
            │
            ▼
   Risk Engine                  (Phase 9)  APPROVED / REJECTED / PAUSED
            │
            ▼
   Execution Adapter            (Phase 8)  Paper today, Live behind a flag (17)
            │
            ▼
   Portfolio & P&L              (Phase 10) positions, equity, drawdown
            │
            ▼
   API + WebSocket  →  Dashboard (Phases 12-13)
```

The Risk Engine sits *between* strategy and execution deliberately: a strategy
can never reach an execution adapter without a risk decision.

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
| `trading_bot.monitoring` | Market selection, per-market statistics, terminal view | marketdata, exchange, config |
| `trading_bot.api` | HTTP contract for the dashboard | config, db |
| `trading_bot.main` | Composition root: wires everything | all of the above |

Dependencies point inward. `core` imports nothing from the project, which is
what keeps strategy code (Phase 5) testable without infrastructure.

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
the market-data service (`make market-data`). The API cannot ask the service
how it is doing, so it judges it by its output - which markets the service
last selected (`markets.is_monitored`) and whether each one's newest stored
quote is recent - rather than by assumption. A strategy
runner joins them in a later phase.

## Status

Built: configuration, logging, database layer with the full 13-table data
model and migrations, retention, the exchange abstraction with a Binance
market-data adapter (spot + USDⓈ-M), the real-time market-data engine and its
service, configurable market selection and per-market monitoring, API
skeleton, health and system-status endpoints, frontend shell, test tooling.

Not built: everything from Phase 5 onward - strategy, cost model, execution,
risk engine, portfolio, dashboard. The system-status endpoint
reports those subsystems as `OFFLINE` with the phase that will implement them.

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
