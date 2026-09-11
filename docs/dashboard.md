# Dashboard

Status: **shell only.** Phase 12 builds the full terminal UI; Phase 13 adds the
real-time backend. The Phase 0 shell renders exactly what
`/api/v1/system-status` reports and invents nothing.

## Non-negotiable rule

Every value shown comes from backend data. No hard-coded prices, P&L, latencies
or market counts. A subsystem that does not exist yet displays `OFFLINE` with
the phase that will build it — which is what the shell does today.

## Design direction

A dark, futuristic quantitative trading terminal: near-black background, glass
panels, restrained neon accents, purple/magenta highlights, green for positive
and red for negative values, dense information, glowing status indicators,
smooth updates. Inspiration only — no branding is copied from any reference.

Phase 0 establishes the CSS tokens (`frontend/src/styles/global.css`); the
visual design is built in Phase 12.

## Planned structure (Phase 12)

**Top navigation** — title, environment (`PAPER`), live indicator, clock,
exchange, market count, feed latency.

**Left panel — bot status** — health of market data, strategy engine, risk
engine, paper execution and database; markets monitored, opportunities
detected, signals generated, paper trades, current exposure. Controls: START,
PAUSE, STOP, KILL SWITCH — each wired to a real backend action.

**Centre — primary market** — BTC/USDT price, 24h change, bid, ask, spread,
volume, live chart, order-book visualization, market activity.

**Right panel — performance** — today's and total P&L, equity, win rate,
trades, profit factor, Sharpe, Sortino, max drawdown.

**Live opportunities** — sortable, filterable, searchable table: market,
strategy, buy, sell, gross edge, fees, slippage, net edge, liquidity, latency,
status (TRADEABLE / WATCH / REJECTED / EXPIRED).

**Market heatmap** — monitored markets with price movement, spread and
opportunity state.

**Equity curve** — paper equity and drawdown over 1D / 7D / 30D / ALL.

**Execution log** — live stream of timestamp, market, event, side, price,
quantity, status, latency, P&L.

**System health** — market data, exchange, database, strategy, risk, execution,
API.

## Backend contract (Phase 13)

```
/api/v1/markets        /api/v1/opportunities   /api/v1/orders
/api/v1/fills          /api/v1/positions       /api/v1/pnl
/api/v1/performance    /api/v1/risk            /api/v1/system-status
```

Real-time updates arrive over WebSocket; the frontend does not poll the
database. Snapshots come from HTTP on load, then deltas stream in.

## Current shell

`frontend/src/` — an API client (`lib/api.ts`), hand-maintained types mirroring
the Pydantic schemas, a status panel, and a dark theme foundation. When the
backend is unreachable it says so and explains how to start it, rather than
rendering empty widgets that look like real data.
