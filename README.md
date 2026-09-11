# Crypto Arbitrage Research Platform

A quantitative research and paper-trading platform for crypto arbitrage on
**binance.com**. The first strategy targets **spot vs perpetual futures** basis
(Phase 5).

This is a research system first and a dashboard second. Correctness, data
quality and realistic execution simulation rank above visuals.

> **Live trading is disabled.** Nothing in this repository can place a real
> order. Phase 17 builds the live-execution path, and it stays off until it is
> explicitly armed. See [Safety](#safety).

Current state: **Phase 1 complete** — foundation and data model. No exchange
connection, no strategy, no trading yet. See
[docs/development-phases.md](docs/development-phases.md).

---

## Requirements

| Tool | Version used | Notes |
| --- | --- | --- |
| Python | 3.12 | Pinned; 3.13+ lacks wheels for some async DB drivers |
| [uv](https://docs.astral.sh/uv/) | 0.9+ | Dependency management |
| Node.js | 20+ | Frontend toolchain |
| pnpm | 10+ | Frontend package manager |
| Docker | 28+ | Runs PostgreSQL locally |

## Quick start

```bash
# 1. Secrets and local settings (never committed)
cp .env.example .env

# 2. PostgreSQL 16
docker compose up -d

# 3. Backend (http://127.0.0.1:8000)
cd backend
uv venv --python 3.12
uv pip install -e '.[dev]'
uv run alembic upgrade head    # create the schema
uv run trading-bot-api

# 4. Frontend (http://localhost:5173) - in a second terminal
cd frontend
pnpm install
pnpm dev
```

Then open <http://localhost:5173>. The shell reports live backend status;
subsystems that later phases build are shown as `OFFLINE`, never faked.

A `Makefile` wraps these commands: `make up`, `make migrate`, `make api`,
`make web`, `make test`, `make check`.

> The database password in `.env` must match the one PostgreSQL was first
> initialised with. If you change it later, reset the volume:
> `docker compose down -v && docker compose up -d`.

## Verifying the setup

```bash
curl http://127.0.0.1:8000/api/v1/health        # liveness
curl http://127.0.0.1:8000/api/v1/health/ready  # 200 healthy / 503 no database
curl http://127.0.0.1:8000/api/v1/system-status # per-subsystem status
open http://127.0.0.1:8000/docs                 # OpenAPI (dev/paper only)
```

## Tests

```bash
make test                                   # backend + frontend
cd backend  && uv run pytest                # 197 tests
cd frontend && pnpm test                    # 9 tests
make check                                  # lint + types + tests
```

Database tests need PostgreSQL running (`docker compose up -d`). Without it
they skip with a reason rather than failing, so the suite stays usable.

## Project layout

```
backend/                  FastAPI service, strategy engine, data pipeline
  src/trading_bot/
    api/                  HTTP layer (routes, schemas, dependencies)
    core/                 configuration and logging
    db/                   engine, sessions, ORM models, retention
  alembic/                database migrations
  tests/                  unit and integration tests
config/                   base.yaml + per-profile overrides (no secrets)
docs/                     architecture and design documentation
frontend/                 React + TypeScript dashboard (Vite)
Product_Requirements/     the original phased specification
docker-compose.yml        local PostgreSQL
.env.example              template for secrets and machine-specific values
```

## Configuration

Three layers, each overriding the one before:

1. `config/base.yaml` — shared defaults, committed
2. `config/<profile>.yaml` — `development`, `paper` or `production`, committed
3. `TB_*` environment variables / `.env` — machine-specific values and **all secrets**

Nested keys use a double underscore: `TB_DATABASE__PASSWORD` sets
`database.password`. Select a profile with `TB_PROFILE`.

Secrets never appear in YAML, source code, or logs — credential-shaped fields
are redacted centrally by the logging pipeline.

## Safety

- Live execution requires **all** of: the `production` profile,
  `TB_EXECUTION__LIVE_ENABLED=true`, a confirmation phrase, and API
  credentials. Any other combination fails at startup rather than trading.
- `development` and `paper` profiles **cannot** enable live execution — the
  settings validator rejects the process.
- If live trading is ever enabled, the API key must **not** have withdrawal
  permission, and should be IP-allow-listed.
- Market data used by Phases 0–16 is public; no API keys are needed.

## Documentation

| Document | Contents |
| --- | --- |
| [architecture.md](docs/architecture.md) | Components, data flow, boundaries |
| [data-model.md](docs/data-model.md) | Database design and traceability chain |
| [strategy.md](docs/strategy.md) | Strategy interface and spot/perp basis logic |
| [execution.md](docs/execution.md) | Paper and live execution adapters |
| [risk-management.md](docs/risk-management.md) | Limits, risk decisions, kill switch |
| [dashboard.md](docs/dashboard.md) | Dashboard structure and design direction |
| [development-phases.md](docs/development-phases.md) | Phase-by-phase status |

## License

MIT — see [LICENSE](LICENSE).
