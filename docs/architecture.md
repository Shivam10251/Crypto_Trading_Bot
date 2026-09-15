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
   Transaction Cost Model       (Phase 6)  fee schedule, both-way slippage,
            │                              funding at settlements crossed
            │                              NET EDGE = gross − all costs
            ▼
   Opportunity Engine           (Phase 7)  every opportunity, as an episode
            │
            ▼
   Risk Engine                  (Phase 9)  APPROVED / REJECTED / PAUSED,
            │                              reserving atomically against the
            │                              same account Phase 8 built
            ▼
   Execution Adapter            (Phase 8)  Paper today, Live behind a flag (17)
            │
            ▼
   Portfolio & P&L              (Phase 10) exits priced from the live
            │                              books, realised P&L from actual
            │                              fills, equity and drawdown
            ▼
   API + WebSocket  →  Dashboard (Phases 12-13)
```

A backtest (Phase 11) runs the same pipeline from the market-data engine down,
with recorded history in place of the WebSocket feed and a virtual clock in
place of the wall clock - see
[The replay boundary](#the-replay-boundary).

Phase 8's paper account gate - cash, inventory, margin, exposure - is no
longer a separate stage between the opportunity engine and the adapter.
Phase 9 sits in front of the adapter and reserves against that same account
atomically, so there is exactly one place that accounts for paper exposure
rather than two that could disagree. On top of that reservation it adds what
Phase 8 could not: a durable `RiskDecision` (`APPROVED`/`REJECTED`/`PAUSED`)
written to `risk_events` *before* an order may be submitted, independent
data-quality and timing checks the strategy's own thresholds do not gate on,
a durable kill switch, and a post-trade review of what execution actually
did. See [risk-management.md](risk-management.md) for the full account.

## Backend layout

| Module | Responsibility | Depends on |
| --- | --- | --- |
| `trading_bot.core.config` | Layered settings, live-trading guards | nothing |
| `trading_bot.core.logging` | Structured logs, credential redaction | config |
| `trading_bot.db` | Engine, sessions, ORM models, run scope, retention | config, logging |
| `trading_bot.exchange` | Venue boundary: adapter interface + normalized models | config, logging |
| `trading_bot.exchange.streaming` | Streaming boundary: stream endpoints + parser contract | exchange |
| `trading_bot.exchange.binance` | binance.com spot + USDⓈ-M market data and streams | exchange, config |
| `trading_bot.marketdata` | Live engine: connections, local books, staleness, snapshots, recorder | exchange, config, db |
| `trading_bot.monitoring` | Market selection, per-market statistics, terminal view | marketdata, exchange, config, strategy |
| `trading_bot.strategy` | Strategy contract, domain types, cost model, basis strategy, runner | exchange + marketdata models only |
| `trading_bot.opportunities` | Episode tracking and the research record | strategy, db |
| `trading_bot.execution` | Bounded dispatcher, account reservations, adapter contract, paper simulator, leg coordination, order/fill/position record | strategy + marketdata models, db |
| `trading_bot.risk` | Pre-trade, admission, exit and post-trade decisions; durable kill switch | execution, config, db |
| `trading_bot.portfolio` | Exit policy, executable valuation, P&L accounting, snapshots (history-read or folded), the realised P&L risk reads | execution, risk, marketdata models, db |
| `trading_bot.backtest` | Virtual clock and replay loop, historical data source, replay market view, funding attribution, run lifecycle, engine, report, CLI | every layer above; nothing depends on it |
| `trading_bot.api` | HTTP contract for the dashboard | config, db |
| `trading_bot.main` | Composition root: wires everything | all of the above |

Dependencies point inward. `core` imports nothing from the project, and
`strategy` imports only normalized domain types - no database, no adapter, no
FastAPI - which is what keeps it testable without infrastructure and runnable
unchanged in backtest, paper and live modes. `monitoring` depends on `strategy`
only to draw it, never the other way round.

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

Two processes run today, sharing configuration and the database: the API and
the market-data service (`make market-data`), which also hosts the strategy
runner, the opportunity recorder, and - since Phase 10 - the portfolio
service's exit and snapshot loops. The portfolio lives there because closing a
position needs the same live book the simulator fills against. The API cannot ask the service how it is
doing, so it judges it by its output - which markets the service last selected
(`markets.is_monitored`), whether each one's newest stored quote is recent, and
how recently an opportunity was recorded - rather than by assumption.

## Status

Built: configuration, logging, database layer with the full 13-table data
model and migrations, retention, the exchange abstraction with a Binance
market-data adapter (spot + USDⓈ-M), the real-time market-data engine and its
service, configurable market selection and per-market monitoring, the strategy
framework with the spot/perpetual basis strategy, the transaction cost model,
the opportunity engine recording every detection to PostgreSQL with the
evidence behind it, the paper execution engine and its order/fill record, the
risk engine gating every order behind a durable decision and a kill switch,
the portfolio subsystem closing positions and computing realised P&L from
actual fills (Phase 10), API skeleton, health and system-status
endpoints, frontend shell, test tooling.

Daily-loss and consecutive-loss limits now operate: Phase 10 supplies the
realised P&L they were waiting for, measured over completed paired trades and
a documented UTC day boundary. When the portfolio service is switched off they
report as unavailable exactly as they did before, never as zero.

Phase 11 adds deterministic historical replay through that same pipeline - one
immutable dataset per run, initial state at any start, run isolation enforced
by the database, strict persistence, settled replay funding cash flows, a
per-component completeness verdict - and opt-in capture of the coherent quote,
depth and funding samples a replay needs.

Not built: the dashboard. The system-status endpoint reports every subsystem
by the durable evidence it wrote, and a not-yet-built one as `OFFLINE` with
the phase that will implement it.

### The exchange boundary

Strategies depend on `ExchangeAdapter` and the normalized models, never on
Binance specifics, so a second venue means writing one adapter. Market-data
methods are abstract because every venue must provide them; optional
capabilities and execution methods raise by default rather than returning an
invented answer. Payload validation happens once, in the venue's mapping layer:
past that point the data is trusted, and anything malformed - crossed book,
zero price, missing field - raises `ExchangeDataError` instead of reaching a
strategy.

### The streaming boundary

Live data splits the same way. A venue implements `MarketStreamSource`: which
WebSocket URLs carry which markets, and how to parse one message into a
normalized `Quote`, `DepthDiff` or `TickerStats`. Everything else lives once, in
`trading_bot.marketdata`, for every venue:

```
StreamConnection (reconnect, heartbeat, idle timeout)
        │ raw message + local receipt time
        ▼
MarketStreamSource.parse  (venue-specific, validating)
        │ Quote / DepthDiff / TickerStats
        ▼
MarketDataEngine  ── LocalOrderBook per market (snapshot + diffs, gap → rebuild)
        │           watchdog (STALE after stale_after_ms)
        ▼
MarketSnapshot  ──▶ snapshot() / updates()   (strategies, monitor, risk)
                ──▶ MarketDataRecorder       (market_data, system_events)
```

A market's book is either `SYNCED` or not published at all; there is no state
in which a consumer receives depth the engine cannot vouch for.

### Monitoring

`trading_bot.monitoring` sits on the engine's read API. `select_universe`
decides what the engine streams (configuration: top N pairs by weaker-leg 24h
volume, or explicit lists); `MarketMonitor` samples every snapshot once a
second into `MarketMetrics` with rolling means and latency percentiles.
Sampling keeps the cost flat as markets are added: a hundred markets cost a
hundred snapshot reads per interval however busy they are.

Staleness has two scopes. A connection silent for `stale_after_ms` (2 s)
makes every market on it stale; a single market is stale on its own only after
`market_silence_ms` (30 s), because the venue pushes only changes and a quiet
market is unchanged rather than stale.

### The strategy boundary

`trading_bot.strategy` sits on the monitor's output and reaches nothing else.
A strategy is handed a `MarketView` per market - snapshot, metrics, spec and,
for perpetuals, funding - and returns opportunities, edges and signals. It has
no adapter, no session and no settings object, so the same instance runs under
backtest, paper and live execution without change.

```
MarketSnapshot + MarketMetrics + FundingInfo
        │
        ▼
StrategyRunner ── per strategy ──▶ on_market_data
        │                            detect_opportunities
        │                            calculate_edge      (CostModel)
        │                            generate_signal
        │                            validate_signal
        ▼
EvaluatedOpportunity  - the signal, or the reason there is none
```

Every opportunity is priced and kept, including the rejected ones: they are the
research dataset, and a list of zero opportunities must be distinguishable from
a broken feed. `CostModel` is an interface, and Phase 6 replaced the implementation behind it
without changing it. Fees come from the published schedule per instrument class
and side of the book; slippage is walked in both directions against the real
depth; funding is charged per settlement actually crossed, not accrued
continuously. What cannot be estimated is refused rather than guessed.

### The research record

`trading_bot.opportunities` turns evaluations into stored rows. The unit is an
**episode** - one contiguous period during which the same strategy sees the
same discrepancy in the same direction - not one row per evaluation: measured
live, per-cycle rows would be 4.0M a day against 81K for episodes, almost all
of them restating the previous second.

```
StrategyEvaluation (every second)
        │
        ▼
EpisodeTracker   opens / absorbs / closes, keeping the BEST moment
        │ closed episodes
        ▼
OpportunityRecorder ──▶ opportunities  (status + every gate it failed)
                    ──▶ signals        (one row per leg)
```

### The execution boundary

`trading_bot.execution` is where `trading_bot.risk` (Phase 9) sits in front
of the adapter. A strategy's validated signal becomes two `OrderRequest`s -
one per leg, never a bundle - because that is the only shape in which leg
risk is expressible: one can fill while the other does not.

```
Signal ──▶ bounded dispatcher ──▶ RiskEngine.evaluate ──▶ APPROVED ──▶ ExecutionCoordinator
                                        │                                  │ both legs at once
                                        ▼                                  ▼
                              REJECTED / PAUSED               ExecutionAdapter ──▶ ExecutionResult
                              durable risk_events row,         PaperExecutionAdapter (Phase 8)
                              no order                         LiveExecutionAdapter  (Phase 17, off)
                                                                    │
                                                                    ▼
                                                          ExecutionAttempt  - hedged, naked by N, or nothing filled
                                                                    │
                                        ┌───────────────────────────┴───────────────────────────┐
                                        ▼                                                        ▼
                          RiskEngine.evaluate_post_trade                              ExecutionRecorder
                          (naked exposure, slippage, latency,                         ──▶ orders + fills + open
                          skew, actual exposure limits)                                   positions, orders.risk_event_id
```

`RiskEngine.evaluate` reserves against the same `PaperAccount` Phase 8 built
- order, position and gross-exposure limits, cash, spot inventory, perpetual
margin and borrow capacity all come from that one reservation, checked
atomically against a durable kill-switch guard so a switch flip cannot race
a concurrent approval. Only once that reservation succeeds *and* the
`APPROVED` decision is durably written to `risk_events` does the dispatcher
call the coordinator; a `REJECTED` or `PAUSED` verdict never reaches it, so
no order row is ever created for a risk refusal.

Approval is not the last word. `RiskEngine.admit` runs inside the
coordinator, in the instant before the adapter is handed anything, and
re-checks everything that can change in the meantime - the kill switch,
signal expiry, and every staleness and latency limit, recomputed from
timestamps against the current clock. A refusal there releases the
reservation and produces no order. Shadow probes reserve against a second,
isolated `PaperAccount`, so research can neither consume nor be blocked by
trading capacity. Aborted reservations remain retryable and must pass every
limit again; after a real settlement, actual notionals are checked for adverse
fill-price breaches. See [risk-management.md](risk-management.md) for the full
set of checks, the kill switch's precise guarantees, and how a close differs
from an entry.

The simulator reads the book **at fill time**, after the configured latency
has elapsed, so the market moves before the order lands. Nothing in it is
random: rejections come from venue filters and account constraints, while
partial fills come from observed depth. IOC/FOK are evaluated once at arrival;
GTC is refused until trades and queue position can support it. There is no
fill-probability knob, because a simulator whose disappointments are drawn
from a seed measures the seed.

### The portfolio boundary

`trading_bot.portfolio` is the only thing that closes a position, and the only
thing that says what one earned. It is layered so the arithmetic is testable
without a database and the policy without a market:

```
positions + fills ──▶ accounting.py     pure Decimal P&L, from actual fills
                      statistics.py    win rate, expectancy, drawdown, Sharpe
live books        ──▶ valuation.py      what flattening would really fetch
        │                                    │
        └──────────────▶ exits.py  ◀─────────┘   four reasons to stop holding
                            │
                            ▼
                        closer.py   RiskEngine.evaluate_exit (reduce-only)
                            │       both legs at once, one transaction
                            ▼
                   store.py  ──▶ positions recomputed from their CLOSE fills
                            │
        ┌───────────────────┴───────────────────┐
        ▼                                       ▼
  snapshots.py                            pnl_source.py
  portfolio_snapshots + pnl_snapshots      the realised P&L the Phase 9
  (or an explicit DEGRADED/UNAVAILABLE)    loss limits read
```

Three rules run through it. **Actual fills, never estimates** - a strategy's
expected price survives only as slippage attribution. **PAPER, LIVE and
THEORETICAL never mix**, and `is_shadow` rows are excluded from every
actionable total. **Unmeasured is not zero** - spot borrow, and funding that a
replay cannot observe at settlement, are NULL and named, and no total that
omits them is called complete.

Aggregate equity is all-or-nothing: if one open leg cannot be valued, position
value, unrealized P&L and equity are NULL for that snapshot. `DEGRADED` never
means "equity with a position omitted"; it is reserved for a fully priced but
risky state such as unpaired exposure.

A close is not an entry, and the risk engine treats it differently on purpose:
the kill switch does not block one (a halt stops new exposure; closing removes
it), no capacity is reserved (a close releases rather than consumes), and
reduce-only is enforced independently of the code that computed the quantity.
See [risk-management.md](risk-management.md#closing-a-position) and
[execution.md](execution.md#exits).

An episode that could never be priced is stored as `UNPRICEABLE` with no costs
on it: it happened, so it is counted, and an invented zero would corrupt every
query asking what survived costs. Each row carries the quotes, books, fills,
filters, rates and assumptions behind it, because the raw tables it came from
are purged in days and opportunities never are. Opportunities and signals are
never purged - unlike raw market data, they are the point of the exercise.

### The replay boundary

`trading_bot.backtest` does not contain a strategy, a cost model, a simulator,
a risk engine or a portfolio. It contains what replaces the *live inputs* of
the ones that already exist:

| Live service | Backtest |
| --- | --- |
| `MarketDataEngine` fed by WebSockets | `ReplayMarketData` fed by `HistoricalDataSource` - the same `snapshot` / `execution_snapshot` API |
| wall clock and `asyncio.sleep` | one `ReplayClock` and its virtual `sleep`, injected into every clock parameter |
| concurrent loops (strategy, workers, exits, snapshots) | one driver running the same steps in a fixed order per virtual instant |
| `ExecutionMode.PAPER`, rows outside any run | `ExecutionMode.BACKTEST` and `backtest_run_id` on every row |
| random episode, claim and kill-switch identities | identities derived from the configuration hash |
| funding polled over REST | recorded funding observations |

```
HistoricalDataSource ── one REPEATABLE READ snapshot ──▶ EventValidator ──▶ ReplayMarketData
   (postgres: market_data, order_books,     (dup / regression / gap /       │ applies an event only
    funding_observations, capture gaps)      corrupt / temporal)            │ once virtual time reaches
   initial state before the start ─────────────────────────────────────────▶ its local receipt time
ReplayClock ◀─ advance_to ── BacktestEngine: monitor ─▶ portfolio snapshot ─▶ exits ─▶ strategy
     ▲                        ─▶ risk ─▶ paper execution ─▶ strict flush ─▶ integrity check
     └── wake_next ── ReplayEventLoop (only when nothing is runnable and no DB I/O is in flight)
HeartbeatTask (wall clock, own task) ── liveness, cancel, lost-row detection
```

**Time.** Components already took injected clocks; the replay gives them all
one. The only concurrency left is inside an execution, where both legs sleep
their own simulated latency - and `ReplayEventLoop` advances virtual time only
at quiescence, so a second leg can never measure its latency from the first
leg's arrival, and a query in flight never lets time move. The rest is
serialized, which is a declared execution-model limitation of every run, not a
parity claim.

**Look-ahead.** Events are available at their local receipt time, never the
exchange's, and a read is refused unless every event up to now is provably
buffered; the refusal is recorded, so a reader that swallows it cannot hide it.
The paper adapter reads the book at arrival, so fills price on the book
recorded by then and never a later one. A run starting between samples opens
with the newest valid quote, book and funding observation received *before* the
start, within their carry limits.

**One dataset per run.** Coverage, initial state and every page are read inside
one read-only repeatable-read transaction, so rows inserted or purged while a
run streams are invisible to it. The fingerprint covers reference data,
initial state, every accepted event of the whole range and every rejection.

**Isolation.** Every result row a run writes carries its run, a CHECK ties that
to `mode = 'BACKTEST'`, uniqueness keys include the run, and every store and
report filters on it (`db.scope.RunScope`). Provenance links - signal,
risk event, order, fill, position, P&L row - are foreign keys on
`(parent id, run_key)`, so the database refuses a link across runs or between
a run and paper rows from any writer. Live status and research queries exclude
replayed rows. Deleting a run deletes its artifacts.

**Strictness.** The live components keep their survival behaviour; replay
checks the counters they report it through. A record that cannot be written
after bounded retries, a dropped record, a risk decision that could not be
stored, an exit sweep failure, an unexpected exception inside execution, or a
paper account that disagrees with the durable fills fails the run (`FAILED`,
no fingerprint, no verdict). `INCOMPLETE` means the data or the accounting
could not support the result; the per-component verdict says which.

**Determinism.** Same dataset and configuration produce identical results and
identifiers, because virtual time moves only at quiescence, database work is
serialised, ties break on a total key, and every identity that was random is
derived instead. The dataset fingerprint and configuration hash on the run make
"same inputs" checkable.
