# Development Phases

One phase at a time. Each ends with working, tested code and a commit; work
stops for review before the next begins.

| Phase | Scope | Status |
| --- | --- | --- |
| 0 | Architecture and project foundation | **Complete** |
| 1 | Database and data model | **Complete** |
| 2 | Exchange abstraction (market data only) | **Complete** |
| 3 | Real-time market data engine | Not started |
| 4 | Market monitoring | Not started |
| 5 | Strategy framework + spot/perp basis strategy | Not started |
| 6 | Transaction cost model | Not started |
| 7 | Opportunity engine | Not started |
| 8 | Paper execution engine | Not started |
| 9 | Risk engine | Not started |
| 10 | Portfolio and P&L | Not started |
| 11 | Backtest / replay engine | Not started |
| 12 | Real-time dashboard | Not started |
| 13 | Dashboard real-time backend | Not started |
| 14 | Research and strategy analytics | Not started |
| 15 | Strategy expansion | Not started |
| 16 | Performance optimization (measure first) | Not started |
| 17 | Live trading architecture (built, disabled) | Not started |

## Phase 0 — delivered

**Foundation**

- Project structure: `backend/`, `frontend/`, `config/`, `docs/`
- Python 3.12 pinned via uv; backend installed as an editable package
- Frontend: React 18 + TypeScript + Vite, with pnpm

**Configuration**

- Three layers: `config/base.yaml` → `config/<profile>.yaml` → `TB_*` env
- Profiles: `development`, `paper`, `production`
- Frozen settings objects; secrets only in the environment
- Live-trading guards that fail startup outside an armed production process

**Logging**

- structlog pipeline, console for development and JSON for paper/production
- Central redaction of credential-shaped fields
- SQLAlchemy statement noise suppressed unless `echo_sql` is on

**Database**

- Async SQLAlchemy engine and session management, PostgreSQL 16 via Docker
- Alembic wired to application settings; naming conventions fixed
- No tables yet — Phase 1 designs the schema

**API**

- FastAPI app factory with lifespan management and CORS
- `/api/v1/health`, `/api/v1/health/ready`, `/api/v1/system-status`
- Unbuilt subsystems report `OFFLINE` with their phase, never fake health
- Starts even when the database is down; readiness returns 503

**Frontend shell**

- Typed API client, status panel, dark theme tokens
- Clear error state explaining how to start the backend

**Quality**

- 70 backend tests (pytest), 9 frontend tests (vitest)
- ruff lint + format, mypy strict, tsc strict — all clean
- `make check` runs everything

**Deliberately absent:** exchange connection, market data, strategy logic,
cost model, execution, risk engine, portfolio, dashboard. Phase 0 is foundation
only.

## Phase 1 — delivered

- **13 tables** covering the full traceability chain, in `db/models/`
  (market, research, execution, portfolio, events) - one module per concern,
  none over 220 lines
- **One migration** (`create core data model`): 13 tables, 37 indexes, applied
  and verified to downgrade cleanly
- **NUMERIC for all money**; floats only for derived statistics, enforced by a
  test that scans every table
- **Constraints do the validating**: non-positive prices, crossed books,
  over-fills, incomplete closed positions and out-of-range win rates are all
  refused by the database
- **Duplicate protection** as unique constraints on `(mode, client_order_id)`,
  `(market_id, exchange_trade_id)` and `(order_id, exchange_fill_id)`
- **Retention policies** for the three high-volume raw tables, batched, with a
  `trading-bot-purge` command; research and audit tables are never purged
- **Tests**: 197 backend (up from 75) - schema invariants without a database,
  plus PostgreSQL tests for constraints, cascades, the full traceability walk,
  retention, and migration/model parity

Design decisions and rationale: [data-model.md](data-model.md).

## Phase 2 — delivered

- **`ExchangeAdapter`** ABC: six abstract market-data methods every venue must
  provide; optional capabilities (perpetual funding) and all execution methods
  raise by default, so a missing capability fails loudly instead of returning
  something invented
- **`BinanceExchangeAdapter`** for binance.com **spot and USDⓈ-M perpetuals**,
  routing by market type across two hosts with different payload shapes
- **Normalized models** (`Quote`, `OrderBook`, `TradePrint`, `MarketSpec`,
  `FundingInfo`): frozen slotted dataclasses, `Decimal` throughout, invariants
  enforced in `__post_init__` so a malformed quote cannot be constructed
- **`OrderBook.fill_price()`** walks real depth and reports partial fills -
  the foundation for honest slippage in Phases 6 and 8
- **REST client** with retries, exponential backoff, `Retry-After` handling and
  request-weight tracking (Binance bans IPs that ignore 429s)
- **Execution stays disabled**: `place_order`, `cancel_order`,
  `get_order_status` and `get_balances` raise `ExecutionNotEnabledError`;
  streaming raises pointing at Phase 3
- **Schema correction**: `market_data.exchange_timestamp` is now nullable,
  because Binance spot sends no event time (see below)
- **Tests**: 309 backend (from 197). 112 new tests over recorded live payloads
  plus 6 opt-in tests against the real API

### What checking reality changed

Before writing the mapping layer I probed the live API rather than trusting
documentation from memory. Two findings changed the design:

1. **Binance spot `bookTicker` and `depth` carry no exchange timestamp**, while
   USDⓈ-M futures do. Phase 1 had assumed every venue reports its own clock and
   made the column `NOT NULL`. Storing local time there would have fabricated a
   latency measurement, so the column is now nullable, with a check constraint
   ensuring `latency_ms` cannot exist without the clock it derives from.
2. **Futures reject arbitrary depth limits** (`-4021`); only 5/10/20/50/100/
   500/1000 are valid. The adapter snaps the request up and trims the result.

### First live observation

Measured through the adapter on 2026-09-11:

```
SPOT   bid=76997.45  ask=76997.46   spread 0.001 bps
PERP   bid=76964.80  ask=76964.90   spread 0.013 bps   latency 74 ms
BASIS  -32.60 USD = -4.235 bps (perp at a discount)
FUNDING 0.80 bps per 8h
GROSS 4.235 bps - fees 15 bps - buffer 2 bps = NET -12.765 bps
```

A 4 bps basis against 15 bps of round-trip taker fees is **not** an
opportunity. This is the concrete case for building the cost model before
trusting any signal, and for storing rejected opportunities as research data.

## Phase 3 — next

Build the real-time market-data engine: WebSocket connection management,
reconnection with backoff, heartbeats, stale-data detection, message
validation, order-book synchronisation and sequence-gap detection. Starts with
BTC/USDT, then expands to configurable markets. See
[architecture.md](architecture.md).
