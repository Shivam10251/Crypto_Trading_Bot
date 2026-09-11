# Development Phases

One phase at a time. Each ends with working, tested code and a commit; work
stops for review before the next begins.

| Phase | Scope | Status |
| --- | --- | --- |
| 0 | Architecture and project foundation | **Complete** |
| 1 | Database and data model | **Complete** |
| 2 | Exchange abstraction (market data only) | Not started |
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

## Phase 2 — next

Define the `ExchangeAdapter` interface and implement `BinanceExchangeAdapter`
for **market data only** - no order execution. Must cover binance.com spot and
USDⓈ-M futures endpoints, since the first strategy compares the two. See
[architecture.md](architecture.md).
