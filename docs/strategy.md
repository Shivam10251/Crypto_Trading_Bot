# Strategy

Status: **implemented in Phase 5** - the framework and the first strategy.
Detection only: nothing is executed, and nothing is stored until Phase 7.

## Interface

```python
class Strategy(ABC):
    name: ClassVar[str]

    def initialize(self, context: StrategyContext) -> None: ...
    def on_market_data(self, views: Sequence[MarketView], now: datetime) -> None: ...
    def detect_opportunities(self) -> list[Opportunity]: ...
    def calculate_edge(self, opportunity: Opportunity) -> Edge | None: ...
    def generate_signal(self, opportunity: Opportunity, edge: Edge) -> Signal | None: ...
    def validate_signal(self, signal: Signal) -> ValidationResult: ...
    def detection_stats(self) -> DetectionStats | None: ...
```

A strategy may import only normalized domain types. It must not import the
database, the exchange client, FastAPI, or dashboard code. This is what allows
one implementation to run in backtest, paper and live modes unchanged.

Each strategy lives in its own module. Strategies are never merged into one
file, and shared maths belongs in a helper module rather than a base class that
accumulates behaviour - so `Strategy` declares the pipeline and implements none
of it. `StrategyRunner` walks each opportunity through it; the runner is
infrastructure, not strategy logic.

Two deviations from the Phase 0 sketch, both forced by what the platform became:

- **`detect_opportunities` is plural.** Phase 4 made the system monitor fifty
  pairs at once; an API returning one opportunity per cycle would hide the
  other forty-nine.
- **`generate_signal` takes the priced `Edge`.** A signal cannot be decided on
  without knowing what survived costs, and recomputing the edge inside it would
  price the same opportunity twice.

### What a strategy is handed

`MarketView` pairs one market's `MarketSnapshot` with its `MarketMetrics`, its
`MarketSpec` and - for perpetuals - its current `FundingInfo`. Nothing else is
reachable: no adapter, no session, no settings object.

## First strategy: spot vs perpetual basis

Binance quotes BTC on both spot (`BTCUSDT`) and USD-M perpetual futures
(`BTCUSDT` perp). The perp trades at a premium or discount to spot - the
*basis* - which funding payments pull back toward zero.

```
basis = perp_mid - spot_mid
basis_bps = basis / spot_mid x 10_000
```

When the basis is wide enough to cover **all** costs, the trade is: buy the
cheap leg, sell the expensive leg, and close when the basis converges.

Two rules keep the measurement honest:

**Gross edge is mid-to-mid; reaching the legs is a separate cost.** What it
costs to cross to executable prices is slippage, priced by walking the real
depth for the real size. Netting the two together would hide how much edge the
spreads eat - which is exactly what research needs to know.

**Both legs must be usable at the same instant.** A basis computed from a live
spot quote and a stale perpetual one is a measurement error that looks like
free money. A pair whose legs are not both LIVE, both fresh and both backed by
a synchronised book produces nothing at all, and the reason is counted.

### Why this is not free money

The gross basis is not the edge. Against it stand:

- taker fees on **both** legs, twice (entry and exit) - 30 bps at the
  configured rates, which is more than the basis on almost every pair
- slippage on both legs, dependent on available depth
- funding payments while the perp leg is held - signed, since a short
  perpetual *receives* funding when the rate is positive
- latency between observing both books and both legs actually filling
- leg risk: one leg fills, the other does not, leaving naked exposure
- margin requirements and liquidation risk on the perp leg
- **whether the direction is reachable at all**: capturing a perpetual trading
  *below* spot means selling spot, which a cash account cannot do

The strategy asks the cost model for **net** edge and never trades on gross
spread. See [execution.md](execution.md); Phase 6 replaces the cost model's
implementation, not its interface.

### Answers from Phase 5

The open questions were settled with live data rather than assumed. Measured on
binance.com on 2026-09-11 across 50 pairs - see
[development-phases.md](development-phases.md) for the full run.

- **What basis threshold survives costs?** None, in this market. Median gross
  basis 7.6 bps against a 30 bps round-trip fee floor plus a median 7.6 bps of
  round-trip slippage. Median net edge was **-30 bps**.
- **How long does a tradeable basis persist?** Far longer than round-trip
  latency: a direction held for a median of 26 s against a 75 ms median quote
  latency. The basis is not too fast to catch - it is too small to pay for.
- **Is the edge concentrated anywhere?** In the illiquid tail, and in the
  unreachable direction. The only two pairs whose gross basis exceeded fees
  were sub-cent markets (IOST, VTHO) whose perpetuals traded at a discount -
  capturable only by selling spot.
- **Does funding dominate?** Not over short holds: under 0.1 bps per hour on
  majors. It dominates precisely where the basis is widest - IOST paid -7.9 bps
  *per hour*, which is the market pricing the same dislocation.

## Later strategies (Phase 15)

Cross-market arbitrage, triangular arbitrage, futures basis, statistical
arbitrage and order-book microstructure - each as an independent module
implementing the same interface, added only after the first strategy is
validated end to end.

## Honesty rules

- Opportunities are recorded whether or not they were profitable, and every
  rejection carries a reason - `BELOW_MIN_EDGE`, `SPOT_SHORT_UNAVAILABLE`,
  `FUNDING_UNKNOWN`, `STALE_DATA` and the rest.
- A cost that cannot be estimated is refused, not guessed: a perpetual whose
  funding interval the venue does not publish yields no net edge at all.
- A strategy never reports profit; the portfolio module computes P&L from
  simulated or real fills.
- Backtest, paper and live P&L are labelled and never combined.
