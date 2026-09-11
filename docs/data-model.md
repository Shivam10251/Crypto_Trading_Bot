# Data Model

Status: **designed in Phase 1.** Phase 0 ships the conventions and migration
tooling only — `Base.metadata` is deliberately empty, and a test asserts that.

## Traceability requirement

Every trade must be explainable from raw data to profit:

```
market_data → opportunity → signal → risk decision → order → fill → position → P&L
```

Each row therefore carries the id of the row that caused it. Given a fill, it
must be possible to recover the exact quotes and cost assumptions behind it.

## Planned tables

| Table | Purpose |
| --- | --- |
| `markets` | Instrument reference: symbol, venue, type (spot/perp), tick size, fees |
| `market_data` | Normalized top-of-book snapshots with exchange and local timestamps |
| `order_books` | Depth snapshots, sampled rather than continuous |
| `trades_market` | Public trade prints, used for slippage calibration |
| `opportunities` | Every detected opportunity with gross edge, costs, net edge, status |
| `signals` | Strategy output derived from a validated opportunity |
| `orders` | Intended and submitted orders, paper or live, with idempotency keys |
| `fills` | Executions against orders, including partials |
| `positions` | Open and closed positions with entry/exit and fees |
| `portfolio_snapshots` | Periodic cash, exposure and equity |
| `pnl_snapshots` | Realized and unrealized P&L, separated by mode |
| `risk_events` | Every risk rejection, pause and kill-switch trigger |
| `system_events` | Connection loss, reconnects, stale data, restarts |

## Conventions (in place)

- Naming convention fixed in `db/base.py` so Alembic autogenerate produces
  stable, reviewable diffs.
- `TimestampMixin` gives every table `created_at` / `updated_at` in UTC,
  maintained by the database.
- Timestamps are `TIMESTAMPTZ`. Market data keeps both the exchange timestamp
  and the local receipt timestamp so latency is measurable, not inferred.
- Money and prices use `NUMERIC`, never floats. Floats are fine for derived
  statistics, never for balances or fills.
- P&L rows record their mode (`THEORETICAL` / `PAPER` / `LIVE`) so the three
  can never be summed together by accident.

## Retention

High-frequency data is not kept forever:

| Data | Retention intent |
| --- | --- |
| Top-of-book snapshots | Full fidelity for a short window, then downsampled |
| Order-book depth | Sampled snapshots only |
| Opportunities | Kept indefinitely — this is the research dataset |
| Orders, fills, positions, P&L | Kept indefinitely — audit trail |
| System and risk events | Kept indefinitely |

Phase 1 decides the concrete windows and whether partitioning is warranted,
measured against real data volume rather than guessed up front.

## Migrations

Alembic reads the database URL from application settings, so migrations and the
application can never disagree about the target:

```bash
make migrate                        # alembic upgrade head
make revision m="add markets"       # autogenerate
```
