# Data Model

Status: **implemented.** 13 tables, two migrations. Phase 1 built the schema;
Phase 2 corrected the exchange-timestamp assumption after checking the live
Binance API; Phase 3's market-data service is the first writer of `markets`,
`market_data` and `system_events`.

## Traceability requirement

Every trade is explainable from raw data to profit. Each stage stores the id of
the row that caused it:

```
market_data ──┐
              ├─▶ opportunities ──▶ signals ──▶ risk_events ──▶ orders ──▶ fills ──▶ positions ──▶ pnl_snapshots
market_data ──┘   (both legs)                   (decision)     (intent)   (reality)
```

Given any fill, the exact quotes and cost assumptions behind it can be
recovered. `tests/integration/test_data_model.py::TestTraceabilityChain`
inserts the full chain and walks it backwards from a fill to the originating
bid/ask, so the guarantee is tested, not asserted in prose.

A signal that never became an order is equally traceable: the `risk_events` row
records the limit, the observed value and the reason.

## Tables

| Table | Purpose | Key columns |
| --- | --- | --- |
| `markets` | Instrument reference, one row per venue+symbol+type | exchange filters, fees |
| `market_data` | Normalized top-of-book snapshots | `local_timestamp`, optional exchange clock, `latency_ms` |
| `order_books` | Sampled depth snapshots (JSONB) | `depth_levels` |
| `trades_market` | Public trade prints, for slippage calibration | unique `exchange_trade_id` |
| `opportunities` | Every detected discrepancy, with full cost breakdown | `net_edge_bps`, `status`, `uid` |
| `signals` | Intent to trade a validated opportunity | `expires_at` |
| `risk_events` | Every risk decision: approve, reject, pause | `limit_value` vs `observed_value` |
| `orders` | Intended and submitted orders | `client_order_id` (idempotency) |
| `fills` | Executions, including partials | `slippage_bps`, `is_maker` |
| `positions` | Open and closed exposure | realized/unrealized P&L |
| `portfolio_snapshots` | Cash, exposure, equity over time | `equity_usd` |
| `pnl_snapshots` | Performance metrics per window | Sharpe, Sortino, drawdown |
| `system_events` | Connects, gaps, errors, reconciliation | JSONB `context` |

Two legs are first-class on `opportunities` (`market_id` +
`secondary_market_id`) because the first strategy trades spot against
perpetual futures. Both legs' quote snapshots are recorded.

## Design decisions

**Money is `NUMERIC`, never float.** Prices use `NUMERIC(28,12)` — enough for
BTC near 100,000 and for altcoins below 0.00000001. Fiat values use
`NUMERIC(20,8)`, basis points `NUMERIC(14,6)`. Only derived statistics (Sharpe,
win rate, profit factor) are `double precision`, where precision loss is
harmless. A unit test walks every table and fails if money lands in a float
column.

**Enums are `VARCHAR` + `CHECK`, not native PostgreSQL enums.** The database
still rejects invalid values, but later phases will add order states, risk event
types and strategies. Extending a native enum needs `ALTER TYPE`, which cannot
run inside a transaction and makes migrations fragile.

**Both clocks are stored, when the venue provides one.**
`local_timestamp` is always present; `exchange_timestamp` is nullable because
Binance spot `bookTicker` and `depth` send no event time, while USDⓈ-M futures
do (verified against the live API in Phase 2). Putting local time in that
column would fabricate a latency measurement, so a check constraint enforces
that `latency_ms` can only exist alongside an exchange clock. Staleness is
judged on `local_timestamp` - the one clock the venue cannot control.

**Invalid data is refused by the database.** Check constraints reject
non-positive prices, negative sizes and crossed books (`ask < bid`) — a crossed
top-of-book on a single venue means bad data, and admitting it would corrupt the
research dataset. A locked book (`bid == ask`) is unusual but real, so it is
allowed.

**Duplicate protection is a constraint, not a code path.** `orders` is unique
on `(mode, client_order_id)`, so a retry after a timeout cannot create a second
order. `trades_market` is unique on `(market_id, exchange_trade_id)` and
`fills` on `(order_id, exchange_fill_id)`, so a replayed WebSocket message
cannot double-count.

**`mode` is on every result row.** `THEORETICAL`, `PAPER` and `LIVE` are stored
separately in orders, fills, positions and both snapshot tables. Aggregates must
filter on it; mixing the three produces a number that describes nothing.

**Realized P&L is stored, not derived on read.** It depends on the fee and
slippage assumptions in force at the time; recomputing it later against changed
assumptions would rewrite history.

**Deletes protect the audit trail.** Raw market data cascades with its market,
but `positions`, `orders` and `signals` use `RESTRICT` or `SET NULL` — a market
with trading history cannot be deleted out from under it.

## Retention

Raw feeds have a finite life; the research record does not.

| Data | Retention | Why |
| --- | --- | --- |
| `market_data` | 7 days | Several updates/second/market; decisions already preserved in `opportunities` |
| `order_books` | 3 days | Largest rows |
| `trades_market` | 7 days | Kept long enough to calibrate slippage |
| Everything else | Forever | Research dataset and audit trail |

Windows are configurable per profile (`retention` in `config/base.yaml`).
Purges delete in batches of 10,000 so a long purge never holds a table-wide
lock, filter on `local_timestamp` (a bad feed can misreport the exchange clock),
and run via:

```bash
cd backend && uv run trading-bot-purge
```

Partitioning is deliberately *not* used yet. It is the right answer at volume,
but the correct partition key and interval should be chosen against measured
row counts rather than guessed now.

## Indexes

37 indexes, added for known access patterns rather than speculatively:
`(market_id, exchange_timestamp)` for latest quotes, `local_timestamp` for
retention scans, and on `opportunities` by `detected_at`, `status`, `strategy`
and `net_edge_bps` — the last one serves the central research question, "how
many opportunities survived costs?"

## Migrations

Alembic reads the database URL from application settings, so migrations and the
application can never disagree about the target.

```bash
make migrate                        # alembic upgrade head
make revision m="add funding rates" # autogenerate
```

`tests/integration/test_migrations.py` applies the migration to a scratch
database and compares the result against the model metadata, so a model change
without a matching migration fails the suite instead of a deployment. The
downgrade path is tested too — a migration that cannot be undone is a one-way
door.
