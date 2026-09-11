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
| 8 | Paper execution engine | Next |
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

## Phase 8 — next

Paper execution: the simulator that turns a signal into fills, modelling
spread, depth, latency, partial fills and rejection. It is also what settles
the two assumptions Phase 6 had to leave open - whether a maker order fills,
and what a round trip really costs. See [execution.md](execution.md).
