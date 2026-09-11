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
| `trading_bot.db` | Engine, sessions, declarative base | config, logging |
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

Phase 0 runs one process (the API). Later phases add a market-data service and
a strategy runner as separate processes sharing the same configuration and
database.

## Status

Built: configuration, logging, database layer, API skeleton, health and
system-status endpoints, frontend shell, test and migration tooling.

Not built: everything from Phase 1 onward. The system-status endpoint reports
those subsystems as `OFFLINE` with the phase that will implement them.
