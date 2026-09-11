# Development Phases

One phase at a time. Each ends with working, tested code and a commit; work
stops for review before the next begins.

| Phase | Scope | Status |
| --- | --- | --- |
| 0 | Architecture and project foundation | **Complete** |
| 1 | Database and data model | Not started |
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

## Phase 1 — next

Design the schema for the traceability chain (market data → opportunity →
signal → risk decision → order → fill → position → P&L), create the first
migration, decide retention, and write database tests. See
[data-model.md](data-model.md).
