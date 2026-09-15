# Development Phases

One phase at a time. Each ends with working, tested code and a commit; work
stops for review before the next begins.

Phases 6 and 7 were swapped after Phase 5 measured the market. The reasoning is
in [Phase 7](#phase-7--delivered): recording can begin now and be re-costed
later, because an opportunity row keeps the prices behind it - but a day that
was never recorded is gone for good.

| Phase | Scope | Status |
| --- | --- | --- |
| 0 | Architecture and project foundation | **Complete** |
| 1 | Database and data model | **Complete** |
| 2 | Exchange abstraction (market data only) | **Complete** |
| 3 | Real-time market data engine | **Complete** |
| 4 | Market monitoring | **Complete** |
| 5 | Strategy framework + spot/perp basis strategy | **Complete** |
| 6 | Transaction cost model | **Complete** |
| 7 | Opportunity engine | **Complete** |
| 7.5 | Correctness remediation before Phase 8 | **Complete** |
| 8 | Paper execution engine | **Complete** |
| 9 | Risk engine | **Complete** |
| 10 | Portfolio and P&L | **Complete** |
| 11 | Backtest / replay engine | **In review** |
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

## Phase 3 — delivered

Run it with `make market-data` (or `uv run trading-bot-market-data`): a live
terminal view of every configured market, redrawn every second.

- **Streaming boundary** (`exchange/streaming.py`): a venue contributes only its
  stream URLs and a message parser (`MarketStreamSource`). Connections,
  reconnection, staleness and book sync are venue-independent, so strategies
  never touch a WebSocket - one test drives the engine with a made-up venue
- **Binance streams** (`exchange/binance/streams.py`): combined streams for
  `bookTicker`, `depth@100ms` and the 24h `ticker`, split across connections at
  the per-connection stream limit (200) and validated field by field
- **Connection** (`marketdata/connection.py`): capped exponential backoff with
  jitter, protocol ping/pong heartbeat, and an idle timeout - an open socket
  that goes silent is treated as dead
- **Local order books** (`marketdata/order_book.py`): REST snapshot plus
  buffered diffs under Binance's sequencing rules for both venues, gap
  detection, and a rebuild whenever continuity breaks, the book crosses or a
  connection drops. Depth is published only inside the price range the snapshot
  actually covered
- **Engine** (`marketdata/engine.py`): a `MarketSnapshot` per market - best
  bid/ask, sizes, mid, spread, 24h volume, exchange and local timestamps,
  latency, age, book status - and a watchdog that marks markets `STALE` after
  2 s without data. Consumers read `snapshot()` or a conflating `updates()`
  stream, so a slow consumer gets the latest state, never a backlog
- **Persistence** (`marketdata/recorder.py`): quotes sampled once a second into
  `market_data`, written only when they changed; connects, disconnects, gaps,
  staleness, startup and shutdown go to `system_events`
- **Dashboard**: Exchange and Market Data now report real status, judged from
  the newest stored quote - `HEALTHY`, `DEGRADED` naming the quiet market, or
  `OFFLINE` with the command that starts the service
- **Tests**: 425 backend (from 309), 7 of them opt-in live. Parsing and book
  synchronisation are tested against recorded live WebSocket sequences; the
  live suite runs the whole engine against Binance until both books sync

### What checking reality changed

The WebSocket feeds were probed before any parsing code was written:

1. **USDⓈ-M futures split their streams across routes.** `bookTicker` and depth
   are served under `/public`, the 24h ticker under `/market`. On the legacy
   route a `@ticker` subscription is accepted and then nothing is ever sent -
   no error, no close. Routing is explicit, and idle connections time out, so a
   silent feed cannot pass for a quiet market.
2. **Spot `bookTicker` has no event time on the stream either.** Spot quotes
   keep a null exchange timestamp; spot latency is measured from depth and
   ticker events, which carry one.
3. **Futures update ids are not contiguous.** Spot diffs chain on
   `U == previous u + 1`, futures on `pu == previous u`. One rule for both would
   either miss every futures gap or report false ones constantly. Both recorded
   sequences are test fixtures.
4. **A 100-level spot snapshot spans about 12 USD of BTC.** Updates outside a
   snapshot's range describe a book we never saw, so snapshots are 1000 levels
   deep, updates outside the range are ignored, and the book is rebuilt once the
   market moves past it.

### First live observation

The service on 2026-09-11, BTCUSDT:

```
BTCUSDT spot       LIVE  77246.22 / 77246.23  spread 0.001 bps  latency 41 ms  SYNCED 20
BTCUSDT perpetual  LIVE  77208.8  / 77208.9   spread 0.013 bps  latency 57 ms  SYNCED 20
3/3 connections, 0 invalid messages, 0 sequence gaps, clock skew +17 ms
```

Spot latency comes from depth events and the perpetual's from its own quotes;
both include the measured clock skew.

### Limits

- Staleness is per market, from the last message of any kind. A very quiet
  market on a healthy connection reads `STALE` - deliberately, since "no recent
  confirmation" is not safe to trade on.
- The API judges the service by its stored quotes, so it lags reality by up to
  the persistence interval (1 s) and needs the database.
- Trade prints are not streamed yet; 24h volume comes from the ticker.

## Phase 4 — delivered

`make market-data` now chooses its markets, streams them and shows a monitoring
table: 50 spot/perpetual pairs - 100 markets - by default.

- **Market selection** (`monitoring/universe.py`): `markets.selection:
  top_volume` ranks every USDT pair listed on both spot and perpetual by the
  *weaker* leg's 24h quote volume - a basis trade is bounded by its thinner
  market - and monitors the top `count`. Count, volume floor and exclusions
  (stablecoin bases by default) are configuration, never code; the explicit
  symbol lists are always added, and `selection: explicit` keeps the Phase 3
  behaviour
- **Bulk 24h statistics** (`get_daily_stats`): one request per instrument class
  ranks the whole venue (weight 80 spot, 40 futures)
- **Liquidity** (`LocalOrderBook.liquidity`): value resting within ±10 bps of
  the mid, measured from every level the local book knows rather than the 20
  it publishes, and flagged as a lower bound when the band runs past the
  snapshot's price range; plus buy/sell slippage for a 10,000 USDT market order
- **Monitor** (`monitoring/monitor.py`): samples every market once a second into
  `MarketMetrics` - mid, spread in bps and %, 60 s mean spread, 24h volume,
  order-book imbalance within the band and its mean, liquidity, slippage, data
  age and freshness, latency with p50/p95. Windows hold only live samples, so a
  stale quote cannot drag an average
- **Scale**: snapshot requests capped at 4 in flight; 100 markets start on 3
  connections with every book synced in about 5 s
- **Dashboard**: the API reads which markets the service selected
  (`markets.is_monitored`, new migration) - "50 spot / 50 perp" - instead of
  counting configuration, and shows `unknown` rather than a guess when the
  database cannot be read
- **Tests**: 474 backend (from 425), 9 of them opt-in live; 10 frontend

### What checking reality changed

1. **Staleness had to be split in two.** At 100 markets the Phase 3 rule - no
   message for 2 s means stale - fired 924 STALE events in 3.5 minutes:
   mid-cap spot markets routinely go several seconds without a message, because
   Binance pushes only changes. Now a *connection* silent for 2 s makes every
   market on it stale, and a *market* is stale on its own only after 30 s of
   silence (its stream may have died). The same markets afterwards: no STALE
   events at all.
2. **Published depth is too shallow to measure liquidity on BTC.** 20 levels of
   BTC spot span 0.7 bps and 1000 span 33 bps, while 20 levels of LTC already
   span 37 bps. Liquidity therefore comes from the full local book with an
   explicit completeness flag, not from the top of book.
3. **Storage would have reached 17 GB a week.** A `market_data` row costs about
   290 bytes with its indexes; at Phase 3's once-a-second sampling, 100 markets
   write 2.5 GB a day. Sampling is now every 5 s (about 500 MB a day) and the
   API's freshness window is 15 s.
4. **Futures quoted in multiples of the coin do not pair.** `1000PEPEUSDT`
   against `PEPEUSDT` differs by 1000x; exact-symbol matching leaves those nine
   contracts out rather than inventing a basis.
5. **Binance lists a symbol in Chinese characters** (`牛来USDT`, ranked 7th). Its
   streams behave normally, so it is monitored; the terminal pads by display
   width so it cannot break the table's alignment.

### First live observation

2026-09-11, 100 markets:

```
top 50 of 447 spot/perpetual pairs by weaker-leg 24h volume
excluded: 295 without a USDT perpetual, 214 under 1M USDT on the weaker leg,
          90 not trading, 1 stablecoin
100/100 fresh, 3/3 connections, 100/100 books synced, 0 invalid messages
median spread 1.74 bps; 34.4B USDT of 24h volume across the monitored markets
BTCUSDT spot: 8.59M bid / 7.31M ask within ±10 bps; a 10K order slips 0.0 bps
latency p95 about 50 ms on spot, about 200 ms on perpetual quotes
CPU 22-35% of one core
```

### Limits

- Monitoring statistics live in memory; only sampled quotes and events are
  stored. The dashboard phases decide what to expose.
- The ranking is taken once at start; restart the service to re-rank.
- Perpetual quote latency p95 runs about four times spot's. Network, venue
  batching and local load are all candidates; Phase 16 measures before
  guessing.

## Phase 5 — delivered

`make market-data` now evaluates the strategy against every monitored pair and
adds a panel showing what it concluded, ranked by the edge that survives costs.

- **Strategy contract** (`strategy/base.py`): five steps - detect, price,
  generate, validate - declared and implemented by none of them. Shared maths
  lives in helpers rather than a base class that accumulates behaviour, and the
  walk itself is in `StrategyRunner`, which is infrastructure. A strategy is
  handed a `MarketView` per market (snapshot, metrics, spec, funding) and can
  reach nothing else: no adapter, no session, no settings
- **Domain types** (`strategy/models.py`): `Leg`, `Opportunity`,
  `CostBreakdown`, `Edge`, `Signal`, `ValidationResult`, `RejectionReason`.
  Frozen, `Decimal` throughout, no imports outside the normalized vocabulary
- **Spot/perp basis strategy** (`strategy/basis.py`): pairs the monitored
  markets by symbol, requires both legs LIVE, fresh and backed by a
  synchronised book at the same instant, sizes against the thinner leg's real
  depth, and tracks how long each direction has persisted
- **Cost model** (`strategy/costs.py`): the `CostModel` interface plus a
  provisional implementation - fees from configuration, **slippage from walking
  the real book**, **funding from the venue's live rate and its actual
  interval**, entry and exit both charged. Phase 6 replaces the implementation,
  not the interface
- **Bulk funding** (`get_funding_rates`, `get_funding_intervals`): one request
  covers the whole venue at weight 10, so fifty pairs cost one call, not fifty.
  `FundingTracker` polls on a slow cadence and survives a failed poll by
  keeping the last known rates
- **Rejections are the dataset**: every opportunity is priced and kept with the
  reason it was not traded. Zero opportunities and a broken feed never look
  alike - the panel states how many pairs were usable and why the rest were not
- **Tests**: 491 backend (from 474), 14 of them opt-in live; 10 frontend

### What checking reality changed

The venue was probed before any pricing code was written, and the first live
run changed the design again:

1. **Funding is not settled every eight hours.** Measured on binance.com: 467
   USD-M perpetuals settle every 4 hours, 313 every 8, and 2 every hour. Of the
   50 monitored pairs, 26 were 8h, 23 were 4h and one (IOST) was hourly.
   Scaling a rate by an assumed 8h would have **halved** the funding cost on
   nearly half the universe. `FundingInfo` now carries the interval.
2. **The venue does not publish an interval for every symbol, and the missing
   ones are not 8h either.** 101 symbols are absent from `fundingInfo`; by
   their next settlement times, 63 are on the 4h grid, 28 on the 8h and 9 on
   the 1h. There is no safe default, so the interval is `None` and the cost
   model **refuses to price** that market rather than guessing. All 50
   monitored pairs publish one, so nothing is lost in practice.
3. **The first live run reported two tradeable opportunities, and both were
   unreachable.** IOST and VTHO perpetuals traded 90-100 bps *below* spot, and
   capturing that means **selling spot** - which needs inventory or a margin
   borrow a cash account does not have. The strategy was claiming trades it
   could not place. Direction reachability is now a validation gate
   (`allow_spot_short`, default false): the opportunity is still detected and
   priced, because it is research data, but it is never signalled.
4. **On sub-cent markets the tick is a large fraction of the basis.** One spot
   tick is 10.7 bps of the mid on IOST and 17.2 bps on VTHO, against 0.00 bps
   on BTC. A basis measured on those markets carries several bps of
   quantization noise before anything else is considered.

### First live observation

2026-09-11, 50 pairs, 75 seconds:

```
STRATEGY spot_perp_basis   49 priced   tradeable 0   best net +31.86 bps (IOSTUSDT)
costs: taker 10/5 bps spot/perp x2 legs x2 sides, slippage from book x2,
       funding over 1h, buffer 2 bps
49/50 pairs usable   1 stale data

PAIR        DIR         BASIS bps   FEES    SLIP    FUND   BUF   NET bps  HELD s  VERDICT
IOSTUSDT    sell spot       99.95  29.86   44.13   -7.91  2.00     31.86    32.9  spot short unavailable
VTHOUSDT    sell spot       62.34  29.89   56.16  -10.46  2.00    -15.25    63.5  below min edge
TRXUSDT     sell spot       12.66  29.98    3.28   -0.62  2.00    -21.98    65.6  below min edge
BTCUSDT     sell spot        5.80  29.99    0.01    0.04  2.00    -26.25    65.6  below min edge
ETHUSDT     sell spot        5.33  29.99    0.08    0.06  2.00    -26.80    65.6  below min edge
```

Across the 48 pairs priced in that frame: median gross basis **7.6 bps**,
median net edge **-30 bps**, best net +31.9 bps and unreachable. Only 2 pairs
had a gross basis exceeding fees at all, and both needed spot sold short. 47 of
48 had the perpetual at a discount to spot - a market-wide state, not a
per-pair signal.

**The basis is not too fast to catch; it is too small to pay for.** A direction
persisted for a median of 26 s (capped by the 75 s run) against a 75 ms median
quote latency - three orders of magnitude of headroom. What stops the trade is
30 bps of round-trip taker fees against 7.6 bps of basis.

### Limits

- Nothing is stored. Opportunities and rejections live for one evaluation cycle
  and are drawn; Phase 7 persists them, which is what makes the "how many
  opportunities existed?" question answerable over time rather than per frame.
- The cost model is provisional by design. One flat taker fee per instrument
  class stands in for the account's real fee tier, and the exit is charged the
  same slippage as the entry. Phase 6 replaces both.
- Funding assumes the current rate persists across the assumed holding period
  and accrues linearly. Over an hour on majors this is negligible either way;
  on the high-funding tail it is the dominant term and the weakest assumption.
- Leg risk is not modelled. The strategy prices both legs filling at the
  observed depth; one leg filling and the other not is an execution concern
  that Phase 8 measures and Phase 9 limits.
- The API still reports the Strategy Engine as OFFLINE. It genuinely cannot see
  it: the strategy runs inside the market-data process and writes nothing, so
  there is no output to judge it by until Phase 7.

## Phase 7 — delivered

Taken before Phase 6, deliberately. Phase 5 showed costs running about four
times the basis, so a more precise cost model would sharpen that number without
changing the conclusion. The open question was instead *how often* a wide basis
happens at all - and that can only be answered by recording over days. Because
an opportunity row stores the prices, quantity and itemised costs behind it, a
better fee model can be re-applied to everything already recorded; a day that
was never recorded cannot be recovered. Recording first loses nothing.

- **An opportunity is an episode, not a sample**
  (`opportunities/episodes.py`): one contiguous period during which the same
  strategy sees the same discrepancy in the same direction. It opens when the
  discrepancy appears, absorbs every evaluation while it lasts, and closes when
  the direction flips or a leg stops being priceable
- **The row keeps the episode's best moment**, not its first: if the peak never
  cleared costs, no moment did. `detected_at` and `duration_ms` bound when it
  happened
- **`duration_ms` is a lower bound** - first observation to last. The
  discrepancy existed for some unknown time before we first sampled it and
  after we last did, and understating that is better than inventing it. An
  episode seen once is 0 ms
- **Everything is stored, including the rejected** (`opportunities/recorder.py`):
  status, every gate it failed, and the itemised costs. Reasons are stored
  sorted so research can group by them
- **What cannot be priced is not invented**: an episode whose funding interval
  the venue never published has no net edge, and `net_edge_bps` is NOT NULL for
  good reason - a fabricated zero would corrupt every query asking what survived
  costs. Those episodes are counted and logged instead
- **Signals are written per leg**, linked to their opportunity. Batched inserts
  use `sort_by_parameter_order` - without it PostgreSQL may return generated ids
  in any order and every signal would silently attach to the wrong opportunity
- **The API can finally see the strategy** (`api/strategy_status.py`): it reads
  the opportunities the way it reads quotes for the feed, and reports how many
  were recorded and how many survived costs. A strategy recording only
  rejections is working correctly, so that is never flagged as a fault
- **Tests**: 578 backend (from 491), 14 opt-in live; 10 frontend

### What checking reality changed

1. **Per-cycle rows would have been unusable.** Instrumenting a 180 s live run
   across 50 pairs: one row per opportunity per evaluation is 8,378 rows, or
   **4.0 million a day**, almost all restating the previous second. The same
   run is **168 episodes** - 81K rows a day, a fiftyfold reduction and a
   better answer to "how many opportunities existed?" than a count of samples.
   The schema already had `duration_ms`, which is what an episode needs and a
   sample does not; Phase 1 had designed for this.
2. **No episode was shorter than one sample.** The worry was that a basis
   flickering around zero would churn episodes and need a minimum-duration
   filter - which would have biased the dataset toward exactly the long-lived
   opportunities being counted. Measured: 0 of 168 episodes lasted under a
   second, so the filter exists in configuration but defaults to off.
3. **Storing the rejection reason unsorted split the dataset.** The first run
   produced both `BELOW_MIN_EDGE, SPOT_SHORT_UNAVAILABLE` and the same two
   reversed, because the order depended on which gate happened to fail first.
   Research groups by that column, so the same population landed in two
   buckets. Reasons are now sorted.

### First live observation

340 opportunities recorded across several runs on 2026-09-11, queried back out
of PostgreSQL:

```
status      n     avg_net_bps   best_net_bps
REJECTED    340       -34.41          53.89

what killed them                          n
BELOW_MIN_EDGE                          318
SPOT_SHORT_UNAVAILABLE                   22

beat fees   beat fees+slippage   beat everything   total
       30                   22                23     340

where the edge went (bps of notional)
gross  fees  slippage  funding  buffer     net
12.97  29.98    16.27    -0.87    2.00  -34.41

persistence: median 8.4 s, max 146.7 s, 37 seen exactly once
```

**Every opportunity that survived costs was in the unreachable direction.** Of
the 23 with a positive net edge, 22 were rejected as `SPOT_SHORT_UNAVAILABLE` -
they needed spot sold short - and the twenty-third cleared zero but not the
1 bps floor. Phase 5 found this in a single 75-second frame; the stored record
shows it was not a fluke.

Two details the record makes visible that a live panel could not: funding is a
net **credit** of 0.87 bps on average, large enough that 23 opportunities beat
everything while only 22 beat fees and slippage alone; and slippage at 16.27 bps
is now the second-largest cost, over half the size of fees.

### Limits

- Rows carry no link to the `market_data` quotes behind them. The prices are on
  the opportunity row, so fees can be re-costed from it, but slippage cannot be
  re-derived - that needed a book, and retention deletes it after three days.
- The episode's best moment is stored, not its full time series. "How wide did
  it get and for how long" is answerable; "what was its shape" is not.
- Opportunities are never purged, by design. At the measured 81K rows a day
  that is roughly 24 MB a day, or 9 GB a year, and it is the point of the
  system rather than overhead.
- The recorder keeps at most 5,000 queued episodes while the database is
  unreachable, then drops the oldest with a warning rather than growing without
  bound.

## Phase 6 — delivered

The real cost model, behind the `CostModel` interface Phase 5 defined - the
interface did not have to change.

- **Fees follow the published schedule** (`strategy/fees.py`): maker and taker
  per instrument class, with the BNB discount applied at the different rates
  each leg receives. Rates follow the *account's* VIP tier, which Binance only
  reports behind an authenticated endpoint, so they are configuration - and a
  venue that does publish them overrides the configured guess
- **Entry and exit roles are separate configuration**, defaulting to taker on
  both. Charging the maker rate assumes a resting order was hit, which is an
  execution question Phase 8 has to answer rather than a discount the cost
  model may quietly award itself
- **Funding is discrete** (`settlements_crossed`): it settles at fixed times,
  so a position pays only if it is held across one, and the count comes from
  the venue's own `nextFundingTime` and interval
- **The exit is walked, not doubled**: unwinding crosses the spread the other
  way - a bought leg is sold into the bids - so it is a different walk of the
  same book. Phase 5 charged the entry's slippage twice; the strategy now
  prices both sides, falling back to the old assumption only when depth cannot
  fill the unwind
- **Stored opportunities can be re-costed** (`opportunities/recost.py`,
  `trading-bot-recost`): the Phase 7 premise made good. It reports and never
  rewrites - a stored row is what the strategy believed at the time
- **Tests**: 604 backend (from 578), 14 opt-in live; 10 frontend

### What checking reality changed

1. **Spot maker equals spot taker, so limit orders save nothing there.** Both
   are 0.100% at VIP 0 on binance.com. Only the perpetual leg rewards patience
   (0.020% maker against 0.050% taker). The "use maker orders" idea that looked
   promising after Phase 5 is worth about 6 bps of a 30 bps round trip, all of
   it on one leg.
2. **That puts a hard floor on fees, and the floor is above the basis.**
   Cheapest possible round trip - BNB discount, maker on both legs, entry and
   exit - is **18.6 bps**, against an average gross basis of 13.0 bps. No fee
   arrangement available to this account makes the average opportunity viable,
   before slippage is charged at all.
3. **Funding was being charged when none would be paid.** Phase 5 accrued it
   continuously: `horizon / interval` of a settlement. Measured at 16:51 UTC, a
   60-minute BTC hold crosses **zero** settlements while the continuous model
   charges 0.125 of one. It is a payment at a fixed time, not a rate - a
   position that opens and closes between two settlements pays nothing.
4. **Doubling the entry overstated slippage.** With the exit walked against the
   real opposite side of the book, IOSTUSDT's round-trip slippage fell from
   44.13 bps to 30.52 bps in the same market. Doubling was a conservative
   guess; the book had the answer all along.
5. **No fee endpoint is public.** Both `/sapi/v1/asset/tradeFee` and
   `/fapi/v1/commissionRate` require credentials, and `exchangeInfo` carries
   only commission *precision*, not rates. Fees are configuration by necessity,
   which is why the defaults are documented with their source.

### First live observation

The same 50 pairs, with the new model:

```
costs: spot 10/10 perp 2/5 bps maker/taker; taker in / taker out,
       slippage walked both ways, funding at settlements crossed in 1h, buffer 2 bps

PAIR        DIR         BASIS bps   FEES    SLIP    FUND   BUF   NET bps  VERDICT
IOSTUSDT    sell spot       84.37  29.91   30.52   -9.43  2.00     31.36  spot short unavailable
VTHOUSDT    sell spot       47.78  29.95   22.70    0.00  2.00     -6.86  below min edge
TRXUSDT     sell spot       11.16  29.99    3.27    0.00  2.00    -24.11  below min edge
XAUTUSDT    sell spot        5.50  29.99    0.52    0.00  2.00    -27.02  below min edge
```

Most markets now show **zero** funding rather than a smeared fraction: their
next settlement falls outside the one-hour hold. IOST, which settles hourly,
still pays - and receives 9.43 bps for it.

### Re-costing the record

`trading-bot-recost` re-priced all 340 stored opportunities. Under the same
schedule it reproduces the stored numbers to within 0.02 bps, which is the
check that it is arithmetic rather than a new opinion. Under the cheapest
schedule available:

```
re-costed 340 stored opportunities under: spot 7.5/7.5 perp 1.8/4.5 bps (BNB discount applied)
  mean net edge  -34.41 bps -> -23.03 bps
  profitable     23 -> 29
  6 opportunities newly clear costs, best VTHOUSDT at +10.10 bps
```

**And all 29 of them require selling spot short.** Not one is reachable from a
cash account. That is the Phase 6 conclusion, and it is a larger finding than
the cost model itself: **cost is not what blocks this strategy.** Perfect fee
optimisation moves the mean edge from -34 to -23 bps and converts nothing
tradeable into nothing tradeable. The binding constraint is the inability to
short spot, because the basis is almost always in that direction - 47 of 48
pairs in one frame had the perpetual at a discount.

### Limits

- The VIP tier is configuration. With credentials the account's real rates
  could be read and would override it; without them the published VIP 0
  schedule is the assumption.
- The exit is priced against *today's* book. By the time a basis converges the
  book will have moved - this is the best estimate available before Phase 8
  measures real round trips.
- The current funding rate is assumed to hold for each settlement crossed. Over
  an hour that is reasonable; over a day it is the weakest assumption here.
- Charging a maker rate assumes the resting order was hit. Nothing in the cost
  model can know that, which is why the default is taker on both sides.
- Re-costing can re-derive fees only. Slippage needed a book that retention
  deletes after three days, and funding needed the schedule as it was; both are
  carried through unchanged.

## Phase 7.5 — correctness remediation, delivered

Not a new phase in the plan: a prerequisite. An audit of Phases 2-7 found nine
defects that would have made Phase 8's paper fills measure the wrong thing.
Every one was reproduced against the existing code before it was touched.

- **Futures minimum notional was never read.** USD-M `exchangeInfo` publishes
  `{"filterType": "MIN_NOTIONAL", "notional": "50"}`; the parser looked only
  for the spot NOTIONAL filter's `minNotional` key, so every perpetual's
  minimum was `None` and an order under it passed validation. Checked against
  the live venue: all 897 USD-M perpetuals use `notional`, not one publishes
  `minNotional`, and all 3,698 spot symbols use the other shape. The fixtures
  had invented the futures shape, so the tests confirmed the bug
- **`LOT_SIZE` and `MARKET_LOT_SIZE` are now both represented.** A market
  order has to satisfy both, and on every one of the 897 perpetuals
  `MARKET_LOT_SIZE.maxQty` is the tighter cap (BTCUSDT: 120 against 1000). On
  spot the same filter publishes `stepSize: 0` - meaning no constraint - which
  read as a real step would invalidate every quantity
- **The traded quantity is now valid on both legs.** BTC spot steps in
  0.00001 and its perpetual in 0.001, so the shared size was routinely invalid
  on one of them. It is rounded *down* to the two steps' common multiple, and
  both books are re-walked at the rounded size: fills, unwinds, notionals,
  slippage, fees and the edge all follow it down. The requested notional is
  kept alongside so the row shows how much of the intent survived
- **Freshness is per input, not per market.** A 24h ticker arriving refreshed
  a market's aggregate age while the quote and the book being priced against
  went on ageing, so a stale basis could pass a freshness check. Quote age and
  book age are now measured and gated separately, and a funding observation
  older than `max_funding_age_ms` is refused rather than kept indefinitely
- **An unwind the book cannot fill has no price.** The cost model used to fall
  back to charging the entry's slippage twice, which reads as conservative and
  is not: missing exit liquidity is precisely the case where the exit costs
  more than the entry. The opportunity is now refused with
  `UNWIND_NOT_FILLABLE` and stored as an observation, without a manufactured
  exit price
- **Funding is charged on the mark notional.** The venue settles
  `mark x size x rate`; the model was using the perpetual's entry notional,
  which includes the spread we crossed. **And the settlement window is now
  half-open**: `nextFundingTime = T`, `T - 8h` and `T - 24h` describe the same
  8-hourly grid and gave 2, 1 and 1 settlements over twelve hours. They now
  give the same answer, and a settlement count above one is stored flagged as
  an assumption, since the venue announces only the next rate
- **Venue fee overrides reach the calculation.** `FeeSchedule.rate_bps` could
  always prefer a venue-reported rate; nothing ever passed it the `MarketSpec`
  that carries one, so the override was unreachable outside its own unit test
- **A row records when its numbers happened.** An episode keeps its *best*
  moment, and `detected_at` held the *opening* one - so the economics and the
  timestamp on the same row described different instants. `best_observed_at`,
  `last_seen_at` and `samples` are now stored, along with an `evidence` JSONB
  document holding both quotes, both books' sequences and timestamps, the
  levels consumed on entry and unwind, the venue filters applied, the fee
  roles and rates, the funding observation and the cost model's assumptions
- **No column calls an entry price an exit price.** `exit_price` held the
  *sold leg's entry price*. It is no longer written; `sell_entry_price` holds
  that value under its real name and `buy_unwind_price` / `sell_unwind_price`
  hold the modelled exits. A signal's `target_exit_price` is now that leg's
  own unwind rather than the other leg's entry
- **An unpriceable opportunity is stored, not dropped.** It used to be counted
  and discarded, which removed observations from the count of how many
  opportunities existed. It is now a row with NULL costs, a NULL net edge and
  status `UNPRICEABLE`, so research can separate *no opportunity*, *detected
  but unpriceable*, *priced and rejected* and *validated*

**Gross edge is labelled as what it is.** It is a theoretical convergence
edge - what the trade is worth if the two mids meet - not realised profit.
Realised price P&L on a basis position is
`signed_quantity x (entry basis - exit basis)`, and only Phase 8's fills can
supply the second term. The convergence assumption is now an explicit,
auditable setting (`costs.assumed_terminal_basis_bps`, default 0 - unchanged
behaviour, never tuned to flatter the record) and is stored with every row.

**Nothing historical was rewritten.** The 447 opportunities already recorded
keep their values and their meanings; they are told apart from new rows by
`evidence IS NULL`, and `trading-bot-recost` skips and counts rows it cannot
re-price rather than treating their missing costs as zero.

- **Migration**: `c3a7f21b8d46`, additive - five quantity columns on
  `markets`, the provenance and correctly named price columns on
  `opportunities`, cost and net-edge columns relaxed to nullable, and
  `UNPRICEABLE` added to the status vocabulary. **Created but not applied to
  the developer database**, which is still at `b7d41e2a9c3f` pending review
- **Tests**: 659 backend (from 604), 14 opt-in live; 10 frontend

### What checking reality changed

1. **The futures fixture was wrong, so the test agreed with the bug.** Probing
   `fapi/v1/exchangeInfo` on 2026-09-12 showed the real filter shape and also
   that BTCUSDT's perpetual minimum is **50 USDT**, not the 5 the fixture had
   invented - ten times the figure the strategy would have validated against.
2. **The two lot filters are not redundant.** `MARKET_LOT_SIZE.maxQty` differs
   from `LOT_SIZE.maxQty` on all 897 perpetuals and matches on none, while its
   `stepSize` and `minQty` match on all of them. On spot the reverse: the step
   is a disabled `0` on every symbol that has the filter, but the maximum is
   real. Reading either filter alone gets a market order rejected.
3. **The settlement-window bug was worse than it looked.** A twelve-hour hold
   on an 8-hourly market crossed 2 settlements on a fresh poll and 1 on a
   stale one describing the same schedule - so the cost of a position depended
   on how recently funding had been fetched.

## Phase 8 — delivered

`make market-data` can now simulate what the strategy validates, against the
live book, and store every order and fill it produces.

- **`PaperExecutionAdapter`** (`execution/paper.py`) behind a three-method
  `ExecutionAdapter` protocol. The strategy never chooses an adapter; the
  runtime injects one, which is the entire difference between paper and the
  live adapter Phase 17 will add
- **Nothing in it is random.** Every way an order fails to become a complete
  fill comes from something observed: the venue's own filters, depth that was
  not there, or a book that had gone stale. A simulator whose rejections come
  from a coin flip measures its own seed, and there is deliberately no "fill
  probability" setting
- **The book is read at fill time, not decision time.** The configured
  latency elapses first, so the market moves before the order arrives -
  handing the simulator the decision's book would model a market that
  politely waits
- **Leg risk is a measured outcome** (`execution/coordinator.py`), not the
  footnote it was in Phases 5 and 6. Both legs are submitted concurrently and
  the attempt reports what exposure it actually left: hedged, naked by a
  stated quantity, or nothing filled. Nothing unwinds a half-filled pair -
  that is Phase 9's decision, and inventing a remedy now would hide how often
  it happens
- **Shadow probes** (`execution/shadow.py`), off by default: once per interval
  the best *reachable* rejected opportunity is simulated anyway. Without it
  the simulator would never execute anything, because nothing has ever passed
  validation on this account. A probe is flagged `orders.is_shadow` and must
  be excluded from any question about what the strategy would have earned
- **Every attempt is stored** (`execution/recorder.py`), including the ones
  that filled nothing, with the reason. `orders.opportunity_uid` closes a link
  the traceability chain promised and could not deliver: `signal_id` only
  exists once the episode closes, which is after the order was placed
- **The API reports it honestly**: "no orders" is the normal state of this
  strategy, so the panel reads OFFLINE with the reason rather than as a fault
  - and never HEALTHY on no evidence
- **Migrations** `d8b1c04e7f52`, `f1e2a93c7b10` and `a4c9e8126f30`, additive:
  provenance, stable intent identity, fill evidence, retry-safe indexes, open
  positions and explicit separation of hypothetical shadow exposure

### Correctness remediation before Phase 9

The first Phase 8 implementation was not safe to use as research evidence.
The remediation makes these behavioural changes:

- strategy evaluation only enqueues work; bounded workers execute it, re-check
  signal expiry, and drain accepted work on shutdown
- a stable episode UUID produces deterministic intent, attempt and leg IDs;
  repeated one-second evaluations cannot repeatedly open the same episode
- shadow selection excludes actionable opportunities, so a validated signal
  cannot also execute as a probe in the same cycle
- shadow probes reserve against the same feasibility limits but release the
  reservation after measurement; their tagged positions are never restored
  into the strategy paper account
- IOC is immediate and `PARTIALLY_FILLED` is terminal because its remainder is
  cancelled. GTC is refused: book movement alone proves neither a trade nor
  this order's queue position, so no maker fill is inferred
- execution walks every locally known level. Exhausting Binance's capped
  snapshot is `DEPTH_TRUNCATED` (unknown), not invented zero liquidity
- each leg's adapter exception is recorded without discarding the other leg;
  database retry upserts by stable order/fill identity after uncertain commits
- fill-time book sequence/timestamp, consumed levels, fee rate, expected price,
  intent and attempt provenance are durable; signals are linked after an
  episode closes
- paper cash, inventory, borrow, perpetual margin, per-market position and
  gross exposure are reserved before submission. Open non-shadow paper
  positions are durable and restored on restart. Borrow is never priced as
  zero when its account rate is unknown
- latency supports per-market-type baselines plus deterministic per-order
  jitter, and cross-leg fill-time skew is measured

### What checking reality changed

Three live runs of 110 s each, 50 pairs, shadow probing every 5 s. Two
defects only the live book exposed:

1. **A limit at the strategy's own price is not a maker order.** The
   strategy's executable price is the VWAP of *walking the book*, so it sits
   at or through the far touch by construction. The simulator was resting
   such orders and filling them at their own price as maker fills - awarding
   the cheaper rate to an order that behaved exactly like a market order.
   Corrected: an immediately marketable limit crosses as a **taker** at the
   book's prices. GTC is now refused until trade prints and queue-position
   evidence exist; displayed depth is not treated as a maker fill.
2. **A VWAP is almost never a multiple of the tick.** 10 of 40 limit orders
   were rejected `INVALID_TICK_SIZE`, and because the rejection hit one leg
   and not the other it left **8 attempts half-filled and naked**. The
   coordinator now rounds a limit to the venue's tick, against us - down for
   a buy, up for a sell - so the adjustment never invents a price the book
   did not show.

### First live observation

> Historical exploratory observation only. The raw run artifacts are not
> checked in, and the execution semantics have since been remediated as
> described above. These figures must not be used as regression or acceptance
> evidence; new calibrated runs must persist their configuration and samples.

80 paper orders across the corrected runs, every one a shadow probe, **none
from a validated signal** - the strategy validated nothing in 480, 533 and
480 opportunities respectively, which is exactly what Phases 5 to 7 predicted.

```
entry    orders  filled  partial  maker fills  mean slip vs model  fees
MARKET      38      38       0         0            -0.014 bps    28.47 USD
LIMIT       42      38       4         0            -0.229 bps    29.77 USD

attempts   MARKET 19, 0 unhedged      LIMIT 21, 3 UNHEDGED
```

**The maker rate is not available to this strategy, at all.** Not "rarely" -
never. Zero maker fills out of 42 limit orders, because the price the
strategy wants is by construction a price that crosses the spread. Phase 6
computed an 18.6 bps floor assuming maker on both legs and called it an
assumption; the measurement says the reachable floor is the taker one, 30 bps.
Capturing maker rates would require a different strategy, resting inside or
away from the spread, with a fill rate nobody has measured.

**The cost model's slippage estimate is honest.** Realised slippage against
what the strategy expected averaged **-0.014 bps** on market entries
(sd 0.236, range -0.95 to +0.95 over 42 fills in the first run). Walking the
book for the real size, over a 100 ms latency, is essentially unbiased at
this size on liquid pairs. That validates the Phase 6 entry model and leaves
the *exit* estimate still unmeasured.

**Leg risk is real and it is caused by the limit.** Market entries left
nothing naked in 19 attempts; limit entries left 3 of 21 naked, by 0.32,
0.67 and 1,572 units, because a limit fills only the depth inside its price
and the two legs run out at different points. The safer-looking order type
is the one that breaks the hedge.

### Limits

- **P&L and exits are not computed.** Entry fills now create durable open
  positions and `fills.position_id` is populated. Realised P&L, equity and an
  exit policy remain Phase 10. No number here claims a profit.
- **Nothing closes a position.** `OrderIntent.CLOSE` exists and the adapter
  will simulate one, but no round trip is opened and closed automatically, so
  the *exit* half of the cost model is still an estimate. That is the largest
  remaining gap.
- **A half-filled pair is left naked.** Deliberately: unwinding it is a risk
  decision, and Phase 9 owns it.
- **All 80 orders are probes.** The strategy validated nothing, so the gated
  path has executed exactly zero orders in production. It is covered by
  tests, not by live evidence.
- **Latency is configuration, not a measurement.** Market-type baselines and
  deterministic jitter prevent identical legs, but the distribution still
  needs account-specific empirical calibration.
- **One pair dominates.** The probe picks the best reachable opportunity each
  interval, which was BNBUSDT almost every time, so the fill statistics
  describe a liquid mid-cap and not the universe.

## Phase 9 — delivered

**Not signed off.** The first implementation passed its tests and was still
unsafe in fourteen ways; the audit and the repairs are in
[what the audit found](#what-the-audit-found) below. The phase stays in
review until those repairs have been reviewed, not merely until they are
green.

The risk engine (`trading_bot.risk`) now sits in front of the execution
adapter. Every signal - real or shadow - is evaluated; a `REJECTED` or
`PAUSED` verdict never reaches the coordinator, so no order is ever created
for a risk refusal, and an `APPROVED` verdict is never returned until its
`risk_events` row is durably stored.

- **`RiskEngine.evaluate`** (`risk/engine.py`) checks, in order: the durable
  kill switch (actionable signals only), signal expiry, evidence
  completeness, per-leg quote/book staleness, funding staleness, decision
  latency, expected slippage, the deferred daily-loss/consecutive-loss gates,
  and finally an atomic reservation against `PaperAccount` for order,
  position, exposure, cash, margin and borrow limits. The first failure wins
  and is persisted; nothing after it runs
- **Reused, not reimplemented**: the account reservation Phase 8 already
  built for concurrent execution workers is exactly what a risk engine
  needs, so `RiskEngine.evaluate` calls `PaperAccount.reserve` rather than
  keeping a second, competing ledger. The one new input it needed -
  `KillSwitchState.guard` - is threaded into that same lock-protected
  reservation, which is what makes a kill-switch flip unable to race a
  concurrent approval
- **The durable kill switch** (`risk/kill_switch.py`) derives its current
  state from the most recent `risk_events` row of type `KILL_SWITCH`, so a
  restart cannot disagree with its own audit trail. It fails closed (starts
  active) if that state cannot be loaded, takes effect in-process
  immediately on trigger regardless of whether the audit write has landed
  yet, and only clears on re-arm once that write is confirmed. Controlled by
  `trading-bot-risk kill|rearm|status` - a CLI, not an HTTP endpoint, because
  nothing in this codebase authenticates a caller yet and an unauthenticated
  public re-arm route is explicitly out of bounds
- **Post-trade review** (`RiskEngine.evaluate_post_trade`) looks at what an
  attempt actually did - naked exposure, realised slippage, execution
  latency, cross-leg fill skew - and persists a row only when something
  needs review, never a routine confirmation. Naked exposure trips the same
  kill switch by default (`risk.pause_on_unhedged`), because Phase 8 measured
  it happening in 3 of 21 limit-order attempts
- **Daily-loss and consecutive-loss limits are honestly deferred.** Phase 10
  does not exist yet, so there is no trustworthy realised P&L to gate on. A
  `PnlSource` interface reports `None` until one exists (`NullPnlSource`
  today); the configured policy (`daily_loss_policy`,
  `consecutive_loss_policy`) either leaves the limit unenforced
  (`"deferred"`, the default) or refuses every signal until a real P&L
  source is wired in (`"fail_closed"`) - it is never evaluated against a
  fabricated zero
- **Shadow isolation**: a probe's decision is recorded and queryable
  (`risk_events.is_shadow`), but it never consults the kill switch or the
  loss gates, never receives post-trade review, and never durably mutates
  exposure - it still reserves against `PaperAccount` (to measure real
  feasibility), but the coordinator releases rather than settles it, exactly
  as Phase 8 already did for shadow orders
- **Idempotent by construction**: `PaperAccount.reserve` returns the existing
  reservation for an in-progress repeated `intent_id` after rechecking the
  kill guard, and
  `risk_events` carries `UNIQUE (mode, intent_id, event_type)`, so a retried
  evaluation converges on one row instead of duplicating it
- **`orders.risk_event_id`** links an order back to the decision that allowed
  it - the column existed since Phase 8 but had no writer until now
- **Migration** `7e6346f5153d`, additive: `risk_events.intent_id` (a decision's
  stable identity), `opportunity_uid` (the same provenance-without-a-foreign-key
  pattern as `orders.opportunity_uid`), `is_shadow`, and the unique
  constraint idempotency depends on
- **Tests**: unit coverage for every gate, atomicity under concurrent
  evaluation, fail-closed persistence failure, kill-switch races via
  `PaperAccount`'s `guard` parameter, and post-trade pause; PostgreSQL
  coverage for the idempotency constraint, kill-switch restart restoration,
  and `orders.risk_event_id` linkage

### What the audit found

A green test suite is not evidence of a safe risk engine; it is evidence that
the tests agreed with the code. An audit of the first implementation found
fourteen defects, most of them in the space between "the check passed" and
"the order was sent". Each is now fixed with a test that fails without the
fix.

1. **The kill switch did not reach a running service.** State was loaded once,
   at startup, so a `trading-bot-risk kill` wrote a row that the process
   placing orders would never read. `KillSwitchState.refresh` re-reads durable
   state and the service polls it on `risk.kill_switch_poll_ms` (default 1 s),
   which is what bounds how long a kill takes to take effect.
2. **A kill during approval persistence could still place the order.** The
   window between "reserved and approved" and "submitted" was unguarded.
   There are now three checkpoints: at evaluation, again once the approval is
   durable (which withdraws it and releases the reservation), and
   `RiskEngine.admit` immediately before the adapter is handed anything.
   `PaperAccount.reserve` also checks the guard **before** its idempotency
   shortcuts, so a retried intent cannot ride in on a reservation made before
   the halt.
3. **A kill left accepted work in the queue.** The switch now notifies
   listeners, and `ExecutionDispatcher.purge` drops queued actionable work
   (auditing each item), refuses new actionable work at `enqueue`, and asks
   the adapter to cancel anything submitted and still open. Paper orders are
   cancellable during their simulated-latency window, although none remains
   resting after `submit` returns - see the guarantees in
   [risk-management.md](risk-management.md#kill-switch).
4. **A failed flush lost risk events and reported success.** Every row in a
   flush shares one transaction, so a failure anywhere rolls back all of
   them; the old code requeued only the tail and counted rows before the
   commit. The whole batch is requeued now, and no counter moves until the
   session context manager has exited cleanly.
5. **Ages were read off the row, not measured.** An opportunity records how
   old its inputs were *at detection*; a signal that waited in a queue
   presented those numbers as current. Ages are recomputed from the stored
   timestamps against the current clock at all three checkpoints.
6. **Evidence was checked for presence, not for consistency.** It is now
   validated against the opportunity it claims to describe: same market,
   side and quantity per leg, a quote that is neither non-positive nor
   crossed, a book with a real sequence, no timestamp from the future, and a
   funding observation whenever a perpetual leg is priced.
7. **The loss limits lied in two directions.** An approval said "within every
   configured limit" while two of them had not been evaluated at all;
   approvals now name their `deferred_controls` explicitly. And a breach only
   rejected one signal: a daily-loss breach now halts trading for
   `risk.daily_loss_halt_minutes` and expires on its own terms, while a
   consecutive-loss breach is a durable pause that needs an audited re-arm.
8. **Abnormal execution was recorded but never acted on.** Realised slippage
   or latency past their limits, and adapter failures or timeouts, now pause
   trading under `risk.pause_on_abnormal_execution`, alongside naked
   exposure. Cross-leg skew is the documented exception: recorded, never
   halting on its own.
9. **Shadow probes shared the trading ledger.** A probe in flight could
   reject or delay a real signal competing for the same reservation. Probes
   now reserve against their own `PaperAccount`, which is never restored from
   durable positions and never settled.
10. **The health endpoint still said "not implemented until Phase 9".** It
    now reports what the engine wrote, and a halted kill switch as DEGRADED
    with its reason - even when execution is switched off, because that halt
    is what a restart would restore.
11. **`risk_events.signal_id` was never populated.** Decisions are made
    before their signal rows exist, so they are back-linked when the episode
    closes, the same way orders and positions already were.
12. **The execution queue had two sizes.** `risk.max_execution_queue_size`
    duplicated `execution.queue_size` and could disagree with the queue
    actually in use; it is gone, and the audit row reports the dispatcher's
    own bound.
13. **Slippage meant two different things.** The pre-trade gate summed both
    legs while the post-trade review checked each leg separately. Both now
    use one definition: adverse slippage, summed across the legs.
14. **A failed kill exited zero**, telling an operator trading had stopped
    when no service would ever see it. Both directions exit nonzero now. The
    kill/re-arm writes serialize on a PostgreSQL advisory lock before
    insertion, and restoration orders those transitions by `id`, so neither
    clock skew nor concurrent sequence allocation decides which state is current.

The repair review found and closed four further safety defects: polling could
clear an in-process kill while its write was in flight; funding was incorrectly
held to the two-second quote/book age despite its sixty-second polling cadence;
aborted reservations were marked completed and could bypass resource limits on
retry; and concurrent workers overwrote each other's in-flight cancellation
entries. Evidence validation now also ties the signal's entry and unwind
prices to complete book walks, rejects malformed/non-finite or timezone-naive
inputs without dropping the audit decision, and actual settled notionals are
rechecked after fills. Shutdown waits for kill-triggered adapter cancellation
tasks instead of abandoning them with the event loop.

### What checking reality changed

1. **The account reservation had to move earlier in `ExecutionCoordinator.execute`.**
   The risk engine now reserves before the coordinator's own spot-short and
   signal-expiry checks run; those checks used to run first. Left in the old
   order, a risk-engine reservation that reached one of them would never be
   released - a real resource leak under the exact case this phase adds a
   caller for. `execute` now reserves first and releases on every early
   return.
2. **A structured rejection reason was needed, not a rendered string.**
   `PaperAccount.reserve` explained *why* it refused only in a free-text
   `detail`. Mapping that back to a specific `RiskEventType` (order size vs.
   position vs. gross exposure, all sharing one `RejectionCode`) would have
   meant parsing prose. `AccountRejection` now carries `limit_name`,
   `limit_value` and `observed_value` directly, and the risk engine reads
   those instead.

### Limits

- **Daily-loss and consecutive-loss limits do not operate**, by design, until
  Phase 10 supplies real realised P&L. Their *responses* are built and
  tested - a daily-loss breach halts for a configured period, a
  consecutive-loss breach needs an audited re-arm - but nothing can fire them
  until a real `PnlSource` exists. `"fail_closed"` makes that gap loud rather
  than trading blind through it; it is not the limit "working".
  *(Closed by [Phase 10](#phase-10--delivered): `PortfolioPnlSource` supplies
  the realised P&L, and both limits now fire. They still report unavailable -
  never zero - when the portfolio service is switched off.)*
- **A kill takes up to one poll interval to reach another process.** The
  bound is `risk.kill_switch_poll_ms` (1 s by default), not zero. A kill
  triggered inside the trading process itself is immediate; one written by
  the CLI is not, and a service that cannot reach the database keeps its last
  known state rather than inventing a new one.
- **The kill switch has no dashboard yet.** It is controlled entirely by
  `trading-bot-risk` on the host running the bot. Phase 12 can build an
  authenticated route once one exists; there is deliberately no
  unauthenticated one today.
- **Paper orders never rest after `submit` returns**, but they are tracked and
  cancellable while simulated latency is in progress. Phase 17 must preserve
  this per-order registry for genuinely resting live orders. A kill never
  unwinds exposure that has already filled.
- **The risk engine is single-process.** Its reservations live in one
  `PaperAccount` in one service. Two trading processes against one database
  would share the kill switch but not the exposure ledger; nothing today runs
  that way, and nothing here pretends it would be safe.
- **Nothing here invents an unwind for a naked leg.** The post-trade review
  reports naked exposure and can pause further entries; it never assumes a
  remedy succeeded or attempts one. Deciding what to do about existing naked
  exposure is Phase 10's, not this phase's.
  *(Answered by [Phase 10](#phase-10--delivered): a naked leg is closed as
  `UNPAIRED_RESIDUAL`, priced from the live books like any other exit, and a
  close that creates one trips the same pause.)*

## Phase 10 — delivered

**Reviewed and signed off for paper execution.** The code has been
independently reviewed, corrected, tested and type-checked. No number it
produces has been calibrated against a live run, so this is an engineering
sign-off, not evidence that the strategy is profitable.

The portfolio subsystem (`trading_bot.portfolio`) closes positions, values
what is still open, and computes realised P&L from actual fills. It is what
Phase 9 was waiting for: `max_daily_loss_usd` and `max_consecutive_losses`
now operate instead of reporting as deferred.

```
accounting.py   pure Decimal P&L per position and paired trade - no I/O
statistics.py   pure performance statistics over sets of trades and equity
exits.py        pure exit policy - when to stop holding, and why
valuation.py    executable exit prices from the live books
records.py      read models: a position and an attempt with their fills
store.py        durable reads, claims, recomputation from fills
close_record.py one close leg as the order and fill rows that prove it
closer.py       orchestration: claim, risk, both legs at once, one transaction
snapshots.py    valuing the book, and both snapshot tables
pnl_source.py   the realised P&L the Phase 9 risk limits were waiting for
service.py      two loops, wired by the market-data service
```

- **Exit accounting is recomputed from fills, never incremented.** A
  position's exit columns are derived from the whole set of its `CLOSE` fills
  every time. That makes repeated reconciliation and a restart after durable
  fills converge on the same row - applying a delta twice double-counts,
  recomputing twice does not. A paper fill lost before its transaction commits
  cannot be recovered (see [Limits](#limits-1)). A position already `CLOSED`
  is never revisited, which is where "a later cost model must not rewrite
  history" is enforced rather than promised.
- **The exit policy is four reasons, in priority order**:
  `UNPAIRED_RESIDUAL` (one leg flat, another not - naked exposure),
  `ADVERSE_BASIS` (the basis widened against the entry past a stop),
  `MAX_HOLDING_PERIOD`, and `BASIS_CONVERGED`. Only the last is a *target*,
  and only it requires a complete executable price on both legs; the three
  risk-reducing reasons are attempted even on thin depth, because leaving the
  exposure on is worse. Nothing here decides an exit is profitable -
  convergence says the spread came back, not that the round trip cleared the
  30 bps taker floor Phase 8 measured.
- **Every exit is priced from the current synchronised books**, walked for the
  exact residual quantity on the side that would actually have to trade.
  A book that is missing, unsynced, or older than
  `portfolio.exits.max_book_age_ms` prices nothing; there is no fallback to
  the last price seen.
- **A kill switch does not block a close.** A halt stops *new* exposure;
  closing removes it, so `RiskEngine.evaluate_exit` is deliberately outside
  that gate, and the switch's state is recorded on the decision instead. This
  is not a general bypass: reduce-only is enforced independently, the
  decision must still be durably stored before any order is sent, and a close
  that leaves a leg naked trips the switch under `risk.pause_on_unhedged`
  exactly as an unhedged entry does.
- **Reduce-only is derived and rechecked from freshly locked rows.** `claim`
  row-locks the attempt, verifies that quantity, closed quantity, status and
  claim counter still match the policy's view, and returns the database view
  actually claimed. Only then does `RiskEngine.evaluate_exit` verify mode,
  status, side and size. `closed_quantity <= quantity` independently protects
  the durable record. A live adapter must additionally set the venue's native
  reduce-only flag; Phase 10 has only the paper adapter.
- **Two workers cannot close the same position.** `claim` row-locks every row
  of the attempt and changes its live legs to `CLOSING` in one transaction.
  The loser abandons the whole claim rather than closing one leg. A stale
  caller is also refused if a partial reconciliation changed its open size.
  A terminal partial close releases its remaining claim immediately, while a
  worker that disappears is recoverable after `claim_timeout_ms`.
- **Both legs at once, always.** Exits are submitted concurrently, exactly as
  entries are: a hedge that unwinds in sequence is unhedged in between. A
  close is a MARKET order, because an IOC limit's failure mode on an *exit*
  is the naked exposure the close was called to remove.
- **The account is reduced, not re-grown.** `PaperAccount.settle_exit` gives
  back the entry notional of the quantity closed - the same basis `settle`
  added it on and `restore` seeds from - while cash and inventory move by the
  actual exit notional. Perpetual price P&L settles into cash, and restart
  restoration replays all durable fills before rebuilding open exposure, so
  prior closed trades do not disappear from buying power. Without those two
  pieces gross exposure would accumulate while cash reset after a restart.
- **`NullPnlSource` is replaced by `PortfolioPnlSource`**, which reads
  committed rows only. `max_daily_loss_usd` measures completed **paired
  trades** whose last leg closed inside `[00:00 UTC today, now)`;
  `max_consecutive_losses` counts completed paired trades, not losing legs -
  counting legs would fire the limit on the losing half of every hedged
  trade. A database it cannot read returns `None`, so Phase 9's fail-closed
  policy behaviour is preserved unchanged.
- **`PnlSource` is now asynchronous.** The alternative - keeping a
  synchronous interface backed by a cache some other loop refreshed - would
  have left the daily-loss gate reading a number whose age nothing bounded.
  Its callers were already asynchronous, so the honest signature cost
  nothing; a short TTL (`portfolio.pnl_refresh_ms`) and one refresh lock keep
  the per-signal cost and concurrent database reads bounded. Loss streaks
  read the newest `max_consecutive_losses` completed attempts rather than a
  date cutoff, so a long idle period cannot hide a breach and the query stays
  bounded.
- **Nothing is valued from a stale mark, and partial equity is never
  published.** If any open leg cannot be priced, the snapshot is
  `UNAVAILABLE` with NULL position value, unrealized P&L and equity. The
  equity curve skips it rather than treating missing exposure as zero.
  `DEGRADED` is a fully valued but explicitly risky state, such as an
  unpaired book. Portfolio and per-strategy P&L rows also name unmeasured
  funding or borrow on positions that are still open; incomplete carrying
  costs do not become invisible merely because the trade has not closed.
- **Sharpe and Sortino are annualised from one stated interval.** They are
  computed only from equity snapshots at `portfolio.snapshot_interval_ms`,
  only when the spacing is regular and there are at least
  `min_return_observations` of them, and the interval and observation count
  are stored beside them. Annualising irregular event-level returns as though
  they were daily is the most common way a Sharpe ratio comes to describe
  nothing; a series whose spacing varied is refused, not estimated.
- **Funding and spot borrow are NULL, not zero.** Nothing in this system can
  measure either yet, so every realised figure that omits them names them in
  `unmeasured_pnl` and no total that is missing them is called complete. See
  [Limits](#limits-1).
- **Migration** `e4f70b2c8d13`, additive: the exit half of `positions`
  (closed quantity, weighted exit price and notional, exit fees and slippage,
  signed price P&L, the durable claim, the exit reason, the mark and when it
  was taken), the honesty columns on both snapshot tables
  (`valuation_status`, `unvalued_positions`, `unpaired_positions`,
  `funding_pnl_usd`, `borrow_cost_usd`, `unmeasured_pnl`), the window bounds
  and `scope_key` that snapshot idempotency keys on, and the reduce-only
  CHECK constraints.
- **Tests** cover the pure arithmetic against hand-computed numbers (long and
  short signs, weighted partial fills, fees charged exactly
  once across successive partial closes, slippage never subtracted twice,
  paired aggregation, statistics, drawdown, return sampling and
  annualisation), the exit policy and the executable-exit pricing; PostgreSQL
  coverage for the reduce-only constraints, concurrent claims, retried
  closes, crash recovery, PAPER/LIVE separation, shadow exclusion, snapshot
  idempotency, the UTC day boundary, consecutive-loss counting, both risk
  limits genuinely firing, and every health state.

### What checking reality changed

1. **The entry recorder could resurrect a closed position.**
   `ExecutionRecorder`'s position upsert set every column from the attempt,
   so a retried entry flush landing after a close would write `status = OPEN`,
   `realized_pnl_usd = 0` and the full entry quantity over a settled
   position - exposure the account does not have. The upsert is now
   conditional on the position still being untouched by an exit, and `CLOSE`
   orders never reach it at all.
2. **Account restoration was seeding the wrong quantity.** It restored
   `positions.quantity`, which since this phase can be partly closed. A
   restart would have restored exposure that had already been given back, and
   drifted further on every restart. It now seeds the remaining open size with
   a prorated notional, and includes `CLOSING` rows, whose exposure still
   exists.
3. **`RiskVerdict` needed `is_durable` as well as `is_approved`.** A
   Phase 9 test asked for it and the property had never been added, so the
   suite was red at `1a82004`. The two questions genuinely differ for a
   refusal - a `PAUSED` kill-switch verdict that could not be written still
   halts trading in-process - and a close that must not be sent without a
   record asks the durability question, not the approval one.
4. **"Unpaired" had to be read from the rows, not the fills.** The exposure a
   snapshot counts comes from `closed_quantity`, so deriving naked exposure
   from the fill history could disagree with it. `AttemptRecord.is_unpaired`
   now answers from the same durable quantities the exposure figures use.
5. **Perpetual close P&L was absent from both cash implementations.** The
   position row reported the gain or loss, but `PaperAccount` and snapshot
   cash did not settle it, and restart restoration discarded every closed
   trade. Both now use the signed close-versus-entry cash flow, and restoration
   seeds balances from all durable fills before adding open exposure.
   Fee replay now follows each fill's recorded asset as well, so changing the
   current BNB preference does not rewrite historical cash.
6. **Partial valuation was being published as account equity.** A snapshot
   with one unpriceable position summed the rest and inserted that partial
   number into the return curve. Any unvalued leg now makes aggregate value,
   unrealized P&L and equity NULL; per-strategy unrealized P&L follows the same
   rule.
7. **One leg and unequal residual quantities were classified as paired.** A
   one-leg attempt could become a completed "paired trade," and two non-zero
   but unequal legs hid naked exposure. Completion now requires exactly two
   flat legs, and hedge balance is checked by remaining quantity.
8. **Risk approved a close before the database claim.** A partial fill between
   approval and claim could leave a stale quantity headed for the adapter.
   Claiming now locks and validates current rows first, returns their fresh
   quantities, and a denied risk decision releases only its own claim.

### Limits

- **Funding and spot borrow are not measured.** A perpetual leg held across a
  settlement really pays or receives funding, and a short spot leg really
  accrues borrow interest. Nothing in this system attributes either to a
  position, so both are stored as NULL and named in `unmeasured_pnl`, and
  every realised figure that omits them says so. This is the largest known
  gap in the accounting: on a basis position held for hours, funding is not a
  rounding error, and a "realised P&L" that silently excluded it would be the
  exact dishonesty the rest of this phase is built to avoid.
- **Nothing has been calibrated against a live run.** The exit thresholds
  (`target_basis_bps`, `adverse_basis_bps`, `max_holding_minutes`) are
  defaults chosen to be defensible, not values measured against this market.
  They decide when positions close and therefore what the P&L is; they need
  live calibration before any result from them means anything.
- **Sharpe and Sortino will be NULL in practice for a long time.** They need
  at least `min_return_observations` (30) regularly spaced equity snapshots
  inside the window, and any gap in the snapshot loop breaks the spacing and
  refuses the sample. That is the intended behaviour, not a defect - but it
  means the two ratios are a promise about later, not a number available now.
- **The snapshot loop reads the whole closed history each cycle** to compute
  the all-time window. That is fine at the volumes this system has and will
  not be at scale; the right answer is an incremental aggregate, and it
  should be built against measured row counts rather than guessed now.
- **A close that keeps failing eventually stops being retried**
  (`max_close_attempts`, default 5). The exposure then stays open, the
  snapshot counts it as unpaired, the health endpoint reads DEGRADED and -
  if it is naked - the kill switch is engaged. That is deliberate: retrying
  forever against a venue that will not fill is not safer than telling an
  operator. It does mean naked exposure can persist until someone looks.
- **The portfolio service is single-process, like the risk engine.** The
  durable claim is what makes concurrent closing safe, and it would work
  across processes - but nothing today runs that way, and the `PaperAccount`
  ledger the closer settles against is still one object in one service.
- **Paper fills that were simulated but never recorded are lost on a crash.**
  The adapter's result cache is in memory. A crash between `submit` and the
  transaction that records it leaves the position open, and the next sweep
  simulates the close again. For a simulator that is the correct recovery; a
  live adapter (Phase 17) must reconcile against the venue instead.

## Phase 11 — complete

Built, independently reviewed, hardened against that review (see
[Hardening after review](#hardening-after-review)), tested and type-checked;
the repository's recorded data still cannot exercise it end to end (see its
Limits below). `uv run trading-bot-backtest run` replays recorded market data
through the real pipeline in virtual time.

```
HistoricalDataSource (one REPEATABLE READ snapshot) ─▶ EventValidator ─▶ ReplayMarketData ◀── read at virtual T only
   initial state before the start ─────────────────────────────────────────┘
ReplayClock ── fixed tick grids ─▶ monitor ─▶ snapshot ─▶ exits ─▶ strategy + execution ─▶ strict flush
          (same classes as the live service; only the clock, the feed and the run scope differ)
```

- **One strategy implementation.** `SpotPerpBasisStrategy`, the cost model,
  `StrategyRunner`, `EpisodeTracker`, `RiskEngine`, `KillSwitchState`,
  `PaperAccount`, `PaperExecutionAdapter`, `ExecutionCoordinator`,
  `ExecutionDispatcher`, `PositionCloser`, `PortfolioStore`,
  `PortfolioPnlSource`, `SnapshotWriter` and `PortfolioService` are the live
  service's classes, unchanged in behaviour. A parity test drives the real
  `MarketDataEngine` over fake sockets and shows replay reproduces its
  strategy decisions exactly from what capture would have stored.
- **One virtual clock** (`backtest/clock.py`), injected everywhere a clock
  or a sleep already was. `ReplayEventLoop` wakes a virtual sleeper only when
  no callback is runnable and no replay database I/O is in flight, so both
  legs of an order register their latencies before either arrives, and a
  query in flight never lets time move. Wall-clock timers keep their meaning.
  Nothing in the replay path calls `datetime.now()` or sleeps for real.
- **No look-ahead by construction** (`backtest/market_state.py`). Events are
  ordered by *local receipt* time - never the exchange clock - with a total
  `(receipt time, kind, row id)` tie-break, and applied only when virtual time
  reaches them. A read is refused outright unless the buffer provably holds
  every event up to now. Orders fill against the book recorded by their
  *arrival*; tests put a spectacular book 50 ms after arrival and a worse one
  before it, and the fill takes the worse one.
- **Run isolation** (`db/models/backtest.py`, `db/scope.py`, migrations
  `c8e1f3a5b9d2`, `d4a7c9e2f6b8` and `e8b1f4c7d2a9`). `backtest_runs` holds
  each run's identity, dataset, range, markets, configuration snapshot and
  hash, code revision, dirty-source digest, lifecycle, counts, warnings and
  completeness verdict. Every result table carries `backtest_run_id` (`ON
  DELETE CASCADE`) with a CHECK tying it to `mode = 'BACKTEST'`; unique keys
  include the run; every store filters on mode *and* run; every provenance
  foreign key includes a run key, so the database refuses cross-run links. Two
  runs over the same data write identical client order ids, intent ids and
  snapshot instants side by side.
- **Real enum constraints first** (migration `b5d9e2c4a7f1`). The
  documented `VARCHAR` + `CHECK` enforcement never existed; it does now, with
  existing data validated before installation and a named refusal if any row
  would violate it.
- **One immutable dataset per run** (`backtest/postgres_source.py`):
  coverage, initial state and keyset-paginated pages all read inside one
  read-only repeatable-read transaction, at most one page per table in memory.
  The `EventValidator` counts duplicates, regressions, out-of-order, corrupt
  and temporally invalid rows and gaps, and never repairs any. A sampled book
  is carried at most `max_book_carry_ms`, funding at most
  `max_funding_carry_ms`.
- **Capture** (`marketdata/capture.py`, off by default): one coherent sample
  per interval - quotes, synchronised depth and funding in one transaction -
  lost samples recorded as gaps, cadence validated against freshness limits.
- **Funding attribution and settlement** (`backtest/funding.py`): signed
  funding for a perpetual leg from recorded settlements, only when every
  crossed settlement has an observation shortly before it, a consistent
  interval and an unambiguous size. Each measured settlement is inserted once
  into `backtest_funding_payments` and posted to the paper account cash before
  snapshots and risk loss checks. Flat legs still copy their total to
  `positions.funding_pnl_usd`; open legs can be accounting-complete to the
  replay end when all due settlements were measured. Otherwise `NULL` plus
  `unmeasured_pnl`, as before. Spot borrow remains unmeasured.
- **Lifecycle**: the uid announced as soon as the row exists, a wall-clock
  heartbeat on its own task, a durable cancel, idempotent terminal
  transitions, `FAILED` with the exception on error, and orphaned `RUNNING`
  rows recovered as `FAILED` - a run is not resumable.
- **Report** (`backtest/report.py`, `report_data.py`, `report_render.py`):
  total return, realised and unrealised P&L, total execution fees by wallet
  and completed-trade fees apart, slippage attribution, funding/borrow, an
  equity reconciliation, full completed-trade statistics and per-strategy
  results, exposure time, turnover, return on peak gross exposure, worst
  unhedged entry, drawdown, Sharpe and Sortino with their sampling interval,
  theoretical opportunity edges kept apart from executed results, rejections
  by reason, latency and slippage distributions, and every dataset issue. A
  derived metric of a run that is not rankable carries its caveat in text and
  JSON alike.

### What checking reality changed

1. **The enum CHECK constraints did not exist.** `enum_types` documented them
   and `create_constraint=True` was never passed, so PostgreSQL accepted any
   string. Fixed before `BACKTEST` was added, as the review asked.
2. **Nothing has ever recorded depth or funding.** `order_books` had no writer
   and funding was never persisted: `trading_bot_dev` holds 34,218 sampled
   quotes and nothing a strategy can price. A replay of 2026-09-11 09:00-10:00
   on a migrated copy of it finishes `INCOMPLETE` (7,152 events, zero
   opportunities, `depth`/`funding_observations` missing) - honestly, and
   identically on a second run. Capture had to exist for replay to mean
   anything.
3. **Legacy orders all have NULL intent keys.** All 80 dev orders predate
   `execution_intent_id`, so re-keying that constraint with `NULLS NOT
   DISTINCT` would have failed on real data. It became partial unique indexes.
4. **The recorded quotes contain duplicates and regressions.** The same window
   has 34 duplicate and 5 regressing quote rows around 09:05-09:08 - two
   market-data processes recording at once. The validator counts and drops
   them; nothing else had ever noticed.
5. **A Phase 10 snapshot could fail forever.** The first snapshot after a
   start writes a P&L row per closed position in one statement; past about a
   thousand positions it exceeds PostgreSQL's 32,767 bind parameters, and
   because nothing advances until a snapshot succeeds, every later one fails
   the same way. Found by the benchmark; inserts are now chunked.
6. **Snapshots were measured quadratic.** `scripts/bench_snapshot_scaling.py`:
   one snapshot cost 49 ms with 250 closed trades, 389 ms with 4,000 and 234 ms
   with 43,200 prior snapshots. Replay folds history in instead
   (`portfolio/incremental.py`): 18 ms at 1,000 trades whatever the snapshot
   count, 44 ms at 4,000. The paper service, a concurrent writer, still reads
   history.
7. **The exit sweep dominated replay time.** Profiling a replay put about two
   thirds of the time in the per-second exit sweep's query on a flat book.
   Skipping the sweep while the run's account holds no exposure took a 2-hour,
   30-trade replay from 78 s to 9 s (~800x real time) with identical results.
8. **Replay cannot see a connection go quiet.** The live engine marks a market
   `NOT_LIVE` after 2 s of connection silence; sampled rows carry no
   heartbeat, so replay rejects the same instant as `STALE_DATA`. The parity
   test pins this divergence rather than hiding it.

### Hardening after review

An independent review listed seventeen defects. Each was verified against the
code before it was changed; the tests named here fail on the pre-hardening code.

1. **Arbitrary start times opened blind.** A run starting between samples had
   no quote, book or funding until the next row arrived. `initial_state` now
   offers, per market and stream, the newest rows received before the start
   within the carry limits; the validator takes the first valid one and the
   market applies it at the start (`test_backtest_dataset_integrity`,
   `test_backtest_results::TestInitialization`, including funding polled
   before the start attributing a settlement 60 s into the run).
2. **The dataset could change under a run.** Coverage and every page used
   separate sessions under `READ COMMITTED`; a capture insert or a retention
   purge mid-run changed what later pages returned. One `REPEATABLE READ READ
   ONLY` transaction now holds for the run; each query is counted as replay
   I/O on its own. A negative control under `READ COMMITTED` reads 183 of 242
   events after a concurrent purge; the snapshot reads all 242
   (`test_backtest_source::TestImmutableView`).
3. **Persistence failures were swallowed.** Recorder flushes logged and
   requeued; a replay could publish a result whose orders never landed. Replay
   now retries each queue at most `persistence_attempts` times and fails the
   run on anything unwritten, dropped, unregistered or decided differently
   because a write failed (risk persist, kill-switch read, P&L view, exit
   sweep), and reconciles the paper account against durable cash, BNB fees and
   per-market exposure at the end. Live behaviour is unchanged; the recorders
   only gained counters (`test_backtest_failures::TestStrictPersistence`:
   transient failure and lost commit acknowledgement converge on identical
   rows; persistent failure, failed final flush, unstored risk decision and a
   divergent ledger each fail with the exact reason).
4. **`execute_inline` swallowed exceptions.** It applied the worker's survival
   rule and reported the work as accepted. It now propagates, and the replay
   coordinator raises adapter exceptions instead of recording `FAILED` legs;
   the episode is never marked executed. Live workers still survive
   (`test_execution_dispatcher::TestInlineExecution`).
5. **No equity baseline.** The first snapshot ran after the first evaluation,
   so a trade on the first tick was inside the baseline and its cost missing
   from drawdown. Snapshots now run before exits and evaluation, on the
   portfolio's epoch grid, with the baseline stamped at its grid floor; the
   terminal valuation is stamped at the end instant when the end falls between
   grid points, and left out of return series
   (`TestEquityBaseline`; incremental and history-read drawdown agree).
6. **Completeness was one list.** Runs now store a per-component verdict:
   dataset (INCOMPLETE if a stream is missing in range *and* at the start, or
   rows were corrupt, out of order, temporally invalid, carried past a limit,
   or lost by capture), accounting (INCOMPLETE if funding or borrow is
   unmeasured - an open perpetual leg counts as measured only if no settlement
   fell while it was held), valuation (INCOMPLETE if the last valuation was not
   complete or fills followed it), persistence (FAILED otherwise). **Decision:
   unreproducible venue filters, sampled depth and serialized scheduling are
   declared execution-model fidelity on every run, not INCOMPLETE** - they are
   true of every run of this engine, and making them INCOMPLETE would make
   INCOMPLETE mean nothing. `performance_rankable` is true only for
   `COMPLETED` (`TestCompleteness`).
7. **The fingerprint omitted observable fields.** Exchange timestamps, volume,
   index price, level counts, rejected-row payloads and the request itself were
   missing, and it covered only events the replay reached. It now covers the
   requested range and markets, reference data (with each market's version),
   initialization, every accepted event of the whole range and every rejection,
   field-delimited; counters are `initialization_events`, `events_accepted`,
   `events_rejected`, `events_replayed` (`TestFingerprint`, one case per field).
8. **Fees were understated.** The report summed only completed trades' fees and
   final equity omitted BNB-paid fees. It now reports every non-shadow fill's
   fee split by wallet, completed-trade fees apart, slippage as attribution
   only, subtracts BNB fees from economic equity, and reconciles final equity
   (`TestReportAccounting`: open at end, one-leg and unequal partial fills,
   entry fees with no trade, BNB-paid fees).
9. **Metrics.** Average trade/win/loss, gross profit/loss, win/loss/breakeven
   counts, per-strategy results, exposure time, turnover, return on peak
   gross exposure, worst unhedged entry, theoretical edges apart.
10. **Lifecycle races.** A cancel before start crashed `mark_running`; a late
    cancel crashed `finish`; a slow run could be recovered as an orphan while
    alive. Transitions are now conditional and idempotent, the heartbeat has
    its own wall-clock task, a `LOST` row stops the run without overwriting
    it, and the CLI announces the uid immediately (`TestLifecycleRaces`,
    `test_run_announces_its_uid_before_replaying...`).
11. **Cross-run links were possible.** Migration `d4a7c9e2f6b8` adds `run_key`
    and composite foreign keys for the provenance links; PostgreSQL refuses a
    cross-run or paper-to-run link from any writer
    (`test_backtest_run_links`: 35 cases; linked rows survive upgrade and
    downgrade in `test_migrations`).
12. **Capture produced stale, incoherent samples.** Defaults (5 s quotes, 1 s
    books, 2 s freshness) replayed as stale; quotes and books came from
    different writers; a failed write was retried later as if captured.
    Capture now writes one transactional sample, is the only quote writer while
    on, records lost samples as `DATA_GAP` events that make an overlapping
    replay INCOMPLETE, and `Settings` refuses a cadence above half the tightest
    freshness limit. `trading-bot-backtest capture` reports rows and gaps from
    the database (`test_replay_capture`).
13. **Timestamps were trusted.** Replay now rejects naive timestamps, venue
    clocks beyond `max_clock_skew_ms` ahead or `max_exchange_lag_ms` behind,
    funding intervals no venue runs and next settlements already past or
    beyond one interval. **Live change:** the strategy rejects a negative feed
    latency as `CLOCK_SKEW` instead of passing it through `max_latency_ms`.
14. **Scheduling.** Option B: serialization stays and is declared on every
    run and report as an execution-model limitation; no parity claim.
15. **Realism, reconfirmed**: concurrent legs leave at the same virtual instant
    and fill on their own arrival books; cross-leg skew is recorded without
    halting; open positions are valued, never force-closed; plus the existing
    arrival-book, truncated-depth, taker-only, IOC-expiry, signal-expiry and
    isolation tests.
16. **Funding timing was unrealistic.** Funding was attributed only when a
    position closed, so an open perpetual paid nothing until exit and daily
    loss limits could not see open funding losses. A replay funding ledger now
    posts each due settlement at virtual settlement time, records it durably in
    `backtest_funding_payments`, and exposes the cash flow to snapshots and
    daily P&L. A fill at the exact settlement instant is refused as ambiguous.
17. **Initialization could hide valid rows behind ten bad rows.** The pre-start
    query now reads every bounded-lookback candidate per stream; the validator
    chooses the newest valid row instead of trusting an arbitrary limit.
18. **Capture could publish a partial sample.** If any market's quote/book
    snapshot fails during capture, the whole coherent sample is abandoned and
    a gap names the affected market.
19. **Dirty provenance was too coarse.** `backtest_runs` now stores a source
    digest that distinguishes dirty worktrees sharing the same HEAD while
    ignoring local `.claude-flow` bookkeeping; only the digest is stored.

**Verification** (2026-09-15): ruff, format and mypy clean (128 source
files); backend pytest passed locally with 1,041 passed and 322 skipped
(Docker/Postgres-backed tests could not run because the Docker daemon was not
available); frontend build and 10 tests pass. The skipped integration tests
cover migration round trips, run isolation and replay persistence against real
PostgreSQL, so they still need to run in an environment with Docker before a
release tag. Synthetic datasets cover the trade-producing paths; **no complete
real dataset exists; no real-data trade result is claimed.**

### Limits

- **No recorded dataset can exercise the pipeline yet.** Until capture has run,
  every replay of real data is `INCOMPLETE` with no opportunities. All trades
  in the test suite come from clearly synthetic datasets.
- **Sampled books cannot show the market moving inside the latency.** With 1 s
  capture a fill sees the latest sampled book at its arrival, usually the one
  the decision saw. The report counts fills on a newer book so this is visible,
  not assumed away. Capturing the diff stream would fix it at far higher cost.
- **Venue filters are partly unreproducible.** `markets` stores only the
  latest lot/notional/tick filters; price bounds, percent-price bands, maximum
  notional and the spot average-price reference are not checked in replay.
  Declared on every run.
- **Execution is serialised** (declared on every run). No queue, so
  `QUEUE_OVERLOAD` cannot occur, and a second signal in the same tick waits one
  execution's latency (and may expire).
- **Connection staleness is not modelled**; component ages are.
- **Spot borrow is never measured.** Funding is measured only when recorded
  settlement observations exist; a run ending with an open perpetual is
  accounting-complete only through the settlements observed up to its end.
- **Runs are not resumable**, and cancellation is observed per heartbeat.
- **A run holds a database snapshot for its duration**: vacuum cannot reclaim
  dead history rows meanwhile, and a server `idle_in_transaction_session_timeout`
  shorter than a run ends it as `FAILED`.
- **Retention still deletes datasets between runs.** `order_books` are purged
  after 3 days by default; a later rerun of a purged range gets a different
  fingerprint. Depth storage at scale is documented, not built
  ([data-model.md](data-model.md)).
- **Any recorded capture gap in range makes a run `INCOMPLETE`**, whichever
  markets it affected.
- **`execution.timeout_ms` stays wall-clock** in the simulator. The config
  validator keeps it above the maximum latency, so it cannot fire in replay;
  it is a hang guard, not a modelled outcome.
- `core/config.py`, `marketdata/service.py` and `portfolio/store.py` were already over the 500-line
  guideline before this phase; they grew slightly rather than being split here.
