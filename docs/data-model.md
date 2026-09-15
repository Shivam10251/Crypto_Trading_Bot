# Data Model

Status: **implemented.** 16 tables, thirteen migrations. Phase 1 built the
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
existed since Phase 1 but had never had a writer until Phase 9. The ninth
(`e4f70b2c8d13`, Phase 10) adds the **exit** half of `positions` and the
honesty columns both snapshot tables needed - see
[What a position records after Phase 10](#what-a-position-records-after-phase-10).
The tenth (`b5d9e2c4a7f1`, Phase 11) installs the enum CHECK constraints this
document had always claimed existed, and the eleventh (`c8e1f3a5b9d2`) adds
`backtest_runs`, run isolation on every result table, `funding_observations`
and book completeness flags. The twelfth (`d4a7c9e2f6b8`, Phase 11 hardening)
makes every provenance link run-scoped in the database, and the thirteenth
(`e8b1f4c7d2a9`) adds replay funding cash flows plus a dirty-worktree source
digest - see [Backtest runs](#backtest-runs-and-run-isolation).

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
| `positions` | Open, closing and closed exposure | weighted entry/exit, `closed_quantity`, realized/unrealized P&L, `is_shadow` |
| `portfolio_snapshots` | Cash, exposure, equity over time | `equity_usd`, `valuation_status` |
| `pnl_snapshots` | Performance metrics per window | Sharpe, Sortino, drawdown, `scope_key` |
| `system_events` | Connects, gaps, errors, reconciliation | JSONB `context` |
| `funding_observations` | Each perpetual funding poll, as received (Phase 11 capture) | `next_funding_time`, `local_timestamp` |
| `backtest_runs` | One historical replay: identity, dataset, configuration, lifecycle | `run_uid`, `status`, `config_hash` |
| `backtest_funding_payments` | Settled funding cash flows posted during a replay | `position_id`, `settled_at`, `amount_usd` |

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

**Enums are `VARCHAR` + `CHECK`, not native PostgreSQL enums.** Later phases
add order states, risk event types and strategies, and extending a native enum
needs `ALTER TYPE`, which cannot run inside a transaction. Until Phase 11 the
CHECK half did not exist: `create_constraint=True` had never been passed, so
only the ORM refused a bad value and any raw writer could store one. Migration
`b5d9e2c4a7f1` installs one constraint per enum column
(`ck_<table>_<enum>`), after scanning existing rows and refusing - with the
offending values and counts - if any would violate it. Adding an enum member
now needs a migration that recreates those constraints; a unit test and the
catalog parity test fail until it has one.

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
on `(mode, backtest_run_id, client_order_id)` `NULLS NOT DISTINCT`, so a retry
after a timeout cannot create a second order, in or out of a run. `trades_market` is unique on `(market_id, exchange_trade_id)` and
`fills` on `(order_id, exchange_fill_id)`, so a replayed WebSocket message
cannot double-count.

**`mode` is on every result row.** `THEORETICAL`, `PAPER`, `LIVE` and
`BACKTEST` are stored separately in opportunities, orders, fills, positions,
risk events and both snapshot tables. Aggregates must filter on it; mixing
them produces a number that describes nothing. `BACKTEST` rows additionally
carry their run, and aggregates filter on that too.

**Realized P&L is stored, not derived on read.** It depends on the fee and
slippage assumptions in force at the time; recomputing it later against changed
assumptions would rewrite history. Phase 10 *does* recompute a position's
accounting - but from the position's own fills, whose fees and prices are what
was actually charged, and never once the position is `CLOSED`. That is the
line: re-deriving the same answer from the same evidence is not a later model
rewriting history, and the `CLOSED` guard is where it is enforced.

## What a position records after Phase 10

A `positions` row used to describe an entry. It now describes a whole round
trip, and every exit column is **recomputed from the position's `CLOSE` fills**
rather than incremented - which makes repeated reconciliation and a restart
after fills were committed converge on one row. A paper fill lost before its
transaction commits is not durable and cannot be recovered from this table.

| Column | Means |
| --- | --- |
| `closed_quantity` | How much of `quantity` has been given back. `quantity - closed_quantity` is the exposure that still exists, and the only quantity an exit may ask for |
| `exit_price` / `exit_notional_usd` | Weighted average of the `CLOSE` fills, and their total |
| `price_pnl_usd` | Signed price P&L on the closed portion, **before** fees |
| `fees_usd` / `exit_fees_usd` | Lifetime fees, and the exit's share of them; entry fees are `fees_usd - exit_fees_usd` |
| `slippage_usd` / `exit_slippage_usd` | Attribution only - already inside the fill prices, never subtracted from P&L a second time |
| `funding_pnl_usd` / `borrow_cost_usd` | **NULL means applicable but not measured**; zero means not applicable or measured zero |
| `unmeasured_pnl` | Which cash-flow components a realized figure is missing |
| `mark_price` / `marked_at` | The last honest mark, and when. A stale or missing mark leaves the previous pair alone rather than refreshing it |
| `close_intent_id` / `close_claim_id` / `close_claimed_at` / `close_attempts` | The durable claim two workers cannot both take; each new claim has a deterministic sequence identity |
| `exit_reason` | Which exit condition fired |

`PositionStatus` gains `CLOSING`: an exit has been claimed and may be in
flight. The exposure still exists - a `CLOSING` position is restored into the
paper account exactly like an `OPEN` one - but no second worker may claim it.

Reduce-only is a constraint, not a convention: `closed_quantity >= 0`,
`closed_quantity <= quantity`, and `status <> 'CLOSED' OR closed_quantity =
quantity`. No sequence of closes can give back more than was opened, reverse a
position, or mark one closed while it still carries size.

## A valuation that could not be made is not a zero

`portfolio_snapshots.position_value_usd` and `equity_usd` are **nullable**, and
`valuation_status` says which of three things happened: `COMPLETE` (every open
position priced from a synchronised book), `DEGRADED` (the book is fully
priced but carries an explicit problem such as unpaired exposure), or
`UNAVAILABLE` (at least one position could not be priced, so no aggregate
position value or equity is published and the equity curve skips the row).
`unvalued_positions` says how many marks were missing. `unpaired_positions`
counts attempts whose hedge is missing or unequal - real naked exposure.

`pnl_snapshots.unrealized_pnl_usd` is also NULL when any open position in its
scope cannot be marked; it is never coerced to zero. The table states its
window rather than implying it (`window_start` /
`window_end`), names what its realised figure is missing (`unmeasured_pnl`,
with `funding_pnl_usd` / `borrow_cost_usd` NULL), and records what its Sharpe
and Sortino were computed from (`return_observations`,
`return_interval_seconds`) - a CHECK constraint refuses a ratio that does not
state its sampling interval. `scope_key` is the non-null rendering of
(`strategy`, `position_id`) that idempotency keys on, because a unique
constraint over NULLs does not deduplicate in PostgreSQL and snapshot
idempotency has to be a constraint rather than a convention.

## Backtest runs and run isolation

A replay writes the same kinds of rows paper execution does, through the same
stores - and, being deterministic, the *same identities*: two runs over one
week produce identical client order ids, intent ids and snapshot instants.
`mode` cannot keep them apart, so every result table carries
`backtest_run_id`:

| Rule | Enforced by |
| --- | --- |
| `BACKTEST` rows have a run, and only they do | `ck_<table>_backtest_mode_has_run` on every table with `mode` |
| a run's rows go when the run does | `ON DELETE CASCADE` from `backtest_runs` |
| uniqueness per run, unchanged outside one | keys below |
| every read is scoped to one mode and at most one run | `db.scope.RunScope` in every store; `IS NULL` outside a run, never "any run" |

| Old key | Now |
| --- | --- |
| `orders (mode, client_order_id)` | `(mode, backtest_run_id, client_order_id)` NULLS NOT DISTINCT |
| `orders (mode, execution_intent_id, signal_leg)` | partial unique indexes: `(mode, ...) WHERE backtest_run_id IS NULL` and `(backtest_run_id, ...) WHERE backtest_run_id IS NOT NULL` |
| `positions (mode, attempt_id, market_id)` | the same pair of partial indexes |
| `risk_events (mode, intent_id, event_type)` | `(mode, backtest_run_id, intent_id, event_type)` NULLS NOT DISTINCT |
| `portfolio_snapshots (mode, captured_at)` | `(mode, backtest_run_id, captured_at)` NULLS NOT DISTINCT |
| `pnl_snapshots (mode, captured_at, window, scope_key)` | with `backtest_run_id`, NULLS NOT DISTINCT |
| `opportunities (uid)` unique index | `(backtest_run_id, uid)` NULLS NOT DISTINCT |

`NULLS NOT DISTINCT` (PostgreSQL 15+) is used only where every other key column
is `NOT NULL`, so rows outside a run deduplicate exactly as before. The two
keys with nullable columns became partial indexes instead: all 80 orders in
the development database predate `execution_intent_id`, and `NULLS NOT
DISTINCT` would have made them collide.

**Links cannot cross a run** (`d4a7c9e2f6b8`). Filtering every read by run
protects the stores; it did not stop *a writer* linking a paper fill to a
backtest order. `opportunities`, `signals`, `risk_events`, `orders`,
`positions`, `fills` and `pnl_snapshots` carry `run_key BIGINT NOT NULL` =
`COALESCE(backtest_run_id, 0)` - set by a `BEFORE INSERT OR UPDATE` trigger
for every writer and pinned by `ck_<table>_run_key_matches_run` - and each
referenced table has `UNIQUE (id, run_key)`. Every provenance foreign key is
composite:

| Link | Foreign key | On delete |
| --- | --- | --- |
| signal → opportunity | `(opportunity_id, run_key)` | CASCADE |
| risk event → signal | `(signal_id, run_key)` | SET NULL (`signal_id`) |
| order → signal | `(signal_id, run_key)` | SET NULL (`signal_id`) |
| order → risk event | `(risk_event_id, run_key)` | SET NULL (`risk_event_id`) |
| fill → order | `(order_id, run_key)` | CASCADE |
| fill → position | `(position_id, run_key)` | SET NULL (`position_id`) |
| position → opportunity | `(opportunity_id, run_key)` | SET NULL (`opportunity_id`) |
| P&L snapshot → position | `(position_id, run_key)` | SET NULL (`position_id`) |

`run_key` is 0 outside a run, so - unlike `backtest_run_id` - it joins a key
that is always checked (a NULL column would make `MATCH SIMPLE` skip it). A NULL
link is still allowed; deletion behaves exactly as before, nulling only the
link column (PostgreSQL 15+ column lists). It is an ordinary column, not a
generated one, because PostgreSQL refuses `SET NULL` on a key containing a
generated column. Existing rows validate unchanged; the migration upgrades and
downgrades without touching data.

`backtest_runs` records a stable `run_uid`, `status` (`PENDING`, `RUNNING`,
`COMPLETED`, `INCOMPLETE`, `FAILED`, `CANCELLED`), the data source, the
requested and actually replayed range, markets, the configuration snapshot and
its SHA-256, the git revision, whether source files were dirty and a source
digest for the dirty tree, wall-clock lifecycle timestamps (created, started,
heartbeat, cancel requested,
completed), a failure reason, counts (`initialization_events`,
`events_accepted`, `events_rejected`, `events_replayed`, evaluations,
opportunities, orders, fills, completed trades - defined in
`backtest.runs.RunProgress`), warnings, dataset issues, a dataset fingerprint
over the complete validated input, and `completeness`: the per-component
verdict (dataset, execution model, accounting, valuation, persistence) and
whether performance is rankable. CHECKs require a
terminal run to have `completed_at`, a started run `started_at`, and a failed
run its reason.

**Downgrading `c8e1f3a5b9d2` refuses while any run exists**; `alembic -x
purge_backtests=true downgrade ...` deletes them deliberately.

### What replay reads

`order_books` gained `bids_complete` / `asks_complete`: a capped snapshot's
last level means "unknown beyond", so a replayed walk past it is
`DEPTH_TRUNCATED`, not "no liquidity". `funding_observations` keeps every
funding poll with its receipt time, unique per `(market_id, local_timestamp)`;
only an observation shortly before a settlement can say what that settlement
charged. Both are written only by opt-in capture, in one transaction per
sample with the quotes of the same instant; a sample that could not be written
is recorded as a `DATA_GAP` system event (component `replay_capture`) and
makes an overlapping replay `INCOMPLETE`. A running replay reads one snapshot,
so retention cannot remove pages it has not read yet; `order_books` retention
(3 days) still deletes a dataset before a long-delayed *rerun*, whose
fingerprint then differs. `funding_observations` is not purged.

**Depth storage at scale.** The upper bound quoted for capture - about 17 GB a
day for 50 markets × 2 sides × 50 levels at 1 s - is JSON text arithmetic
(~40 bytes a level); JSONB and TOAST compression make the stored size smaller
by an amount this repository has **not measured**. Beyond short research
windows, one `order_books` table with row-by-row retention deletes will not
scale. In order of effort: raise `retention.order_books_days` only for windows
you intend to replay and run `trading-bot-backtest capture` to confirm what
landed; partition `order_books` by day on `local_timestamp` so retention drops
partitions instead of deleting rows; store levels as parallel `numeric[]`
price/size arrays (or fixed-point integers) instead of JSONB text pairs;
export closed days to columnar files (Parquet) and give replay a file-backed
`HistoricalDataSource`. None is implemented.

`backtest_funding_payments` is the replay ledger of actual settled funding:
one row per run, position and settlement, idempotent on
`(run_key, position_id, settled_at)`, with the observed rate, mark, quantity
and signed USD cash flow. It is linked to `positions` by the same run key the
rest of the replay provenance uses, so a funding payment cannot point across
runs. The paper account applies the same signed amount before snapshots and
risk loss limits read cash. `positions.funding_pnl_usd` is still set, in a
backtest, for a flat perpetual leg whose every crossed settlement was
determinable; otherwise it stays NULL and named in `unmeasured_pnl`. Open
perpetual legs are complete to date when every settlement up to the replay end
was observed. Spot borrow remains unmeasured.

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
