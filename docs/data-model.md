# Data Model

Status: **implemented.** 13 tables, eight migrations. Phase 1 built the
schema; Phase 2 corrected the exchange-timestamp assumption after checking the
live Binance API; Phase 3's market-data service is the first writer of
`markets`, `market_data` and `system_events`; Phase 4 added
`markets.is_monitored` - the service's latest market selection, which is how
the API knows which markets should be live. The fourth migration
(`c3a7f21b8d46`) added the venue quantity filters an order has to satisfy and
the provenance an opportunity needs to outlive the raw data behind it. The
fifth added paper-order provenance; the sixth (`f1e2a93c7b10`) adds stable
execution-intent/attempt identity, fill-time book evidence, idempotent fill
indexes, signal linkage, and durable open paper positions. The seventh
(`a4c9e8126f30`) marks hypothetical shadow positions so account restoration
and portfolio research cannot silently mix them with strategy exposure. The
eighth (`7e6346f5153d`, Phase 9) gives `risk_events` the stable decision
identity (`intent_id`) a retried evaluation converges on, the same
provenance-without-a-foreign-key pattern as `orders.opportunity_uid`
(`opportunity_uid`), and a shadow flag (`is_shadow`) - `risk_events` had
existed since Phase 1 but had never had a writer until Phase 9.

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
| `risk_events` | Every risk decision: approve, reject, pause | `intent_id`, `limit_value` vs `observed_value` |
| `orders` | Intended and submitted orders | `client_order_id` (idempotency) |
| `fills` | Executions, including partials | `slippage_bps`, fee rate, consumed levels, fill-time book sequence |
| `positions` | Open and closed exposure | realized/unrealized P&L, `is_shadow` |
| `portfolio_snapshots` | Cash, exposure, equity over time | `equity_usd` |
| `pnl_snapshots` | Performance metrics per window | Sharpe, Sortino, drawdown |
| `system_events` | Connects, gaps, errors, reconciliation | JSONB `context` |

Two legs are first-class on `opportunities` (`market_id` +
`secondary_market_id`) because the first strategy trades spot against
perpetual futures.

## Provenance travels with the opportunity

The traceability diagram above has one link it cannot honestly make.
`market_data` holds a quote **sampled every 5 s**, not the quote a decision
priced against, and retention deletes those rows after 7 days while
opportunities are kept forever. A foreign key into it would name an
observation the strategy never saw, and then name nothing.

So `opportunities.evidence` (JSONB) carries the decision's own inputs: both
legs' quotes with their bid/ask/size, local and exchange clocks and sequence
ids; both books' sequence and timestamps; the levels actually consumed on
entry and on the modelled unwind; the venue filters that decided the quantity;
the fee role and rate charged on each leg; the funding rate, mark price, next
settlement, interval and observation time; and the cost model's version and
assumption snapshot. A row stays re-derivable after the raw tables are empty.
`market_data_id` and `secondary_market_data_id` stay for a future writer that
can populate them meaningfully.

**`evidence IS NULL` means legacy**, not lost: rows written before this
existed keep their values and are not backfilled with invention.

## Three timestamps, because they are three facts

An opportunity is an episode, and the row keeps its *best* moment:

| Column | Means |
| --- | --- |
| `detected_at` | when the discrepancy was first observed - the episode opened |
| `best_observed_at` | when the moment the economics describe actually happened |
| `last_seen_at` | the final observation; with `detected_at` it bounds the run |
| `samples` | evaluations absorbed |

`detected_at` alone used to carry all of this, so the numbers and the
timestamp on one row described different instants.

## Prices whose names mean what they say

| Column | Means |
| --- | --- |
| `entry_price` | the **bought** leg's executable entry |
| `sell_entry_price` | the **sold** leg's executable entry |
| `buy_unwind_price` / `sell_unwind_price` | each leg's modelled exit, walked against the other side of its own book |
| `exit_price` | **legacy only.** Held the sold leg's *entry* price - not an exit of anything. No longer written |

`signals.target_exit_price` is now that leg's own modelled unwind; rows
written before 2026-09-12 hold the counterpart leg's entry price there.

## Unpriceable is not zero

An opportunity whose costs could never be estimated - an unpublished funding
interval, or no depth to price the unwind against - is stored with status
`UNPRICEABLE`, NULL costs and a NULL `net_edge_bps`. It used to be discarded,
which removed real observations from the count of how many opportunities
existed; a fabricated zero would have been worse still. A CHECK constraint
enforces that `UNPRICEABLE` and a NULL net edge always agree, so the four
research populations - unpriceable, priced-and-rejected, validated, and
(from Phase 8) paper-executed - cannot blur.

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
| `market_data` | 7 days | One sampled row per market every 5 s (~500 MB/day at 100 markets); decisions already preserved in `opportunities` |
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
