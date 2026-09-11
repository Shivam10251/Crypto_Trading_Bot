# Strategy

Status: **not implemented.** Phase 5 builds the framework and the first
strategy. This documents the intended design.

## Interface

```python
class Strategy(Protocol):
    def initialize(self, context: StrategyContext) -> None: ...
    def on_market_data(self, snapshot: MarketData) -> None: ...
    def detect_opportunity(self) -> Opportunity | None: ...
    def calculate_edge(self, opportunity: Opportunity) -> Edge: ...
    def validate_signal(self, signal: Signal) -> ValidationResult: ...
    def generate_signal(self, opportunity: Opportunity) -> Signal | None: ...
```

A strategy may import only normalized domain types. It must not import the
database, the exchange client, FastAPI, or dashboard code. This is what allows
one implementation to run in backtest, paper and live modes unchanged.

Each strategy lives in its own module. Strategies are never merged into one
file, and shared maths belongs in a helper module rather than a base class that
accumulates behaviour.

## First strategy: spot vs perpetual basis

Binance quotes BTC on both spot (`BTCUSDT`) and USDⓈ-M perpetual futures
(`BTCUSDT` perp). The perp trades at a premium or discount to spot — the
*basis* — which funding payments pull back toward zero.

```
basis = perp_mid − spot_mid
basis_bps = basis / spot_mid × 10_000
```

When the basis is wide enough to cover **all** costs, the trade is: buy the
cheap leg, sell the expensive leg, and close when the basis converges.

### Why this is not free money

The gross basis is not the edge. Against it stand:

- taker fees on **both** legs, twice (entry and exit)
- slippage on both legs, dependent on available depth
- funding payments while the perp leg is held — these can move against you
- latency between observing both books and both legs actually filling
- leg risk: one leg fills, the other does not, leaving naked exposure
- margin requirements and liquidation risk on the perp leg

The strategy therefore asks the cost model for **net** edge, never trading on
gross spread. See [execution.md](execution.md) and the cost model in Phase 6.

### Open questions for Phase 5

These are decided with recorded data rather than assumed now:

- What basis threshold survives costs often enough to be worth trading?
- How long does a tradeable basis persist? If it is shorter than round-trip
  latency, the opportunity is not real for this system.
- Is the edge concentrated in specific hours or volatility regimes?
- Does funding dominate the P&L for holds longer than a few minutes?

## Later strategies (Phase 15)

Cross-market arbitrage, triangular arbitrage, futures basis, statistical
arbitrage and order-book microstructure — each as an independent module
implementing the same interface, added only after the first strategy is
validated end to end.

## Honesty rules

- Opportunities are recorded whether or not they were profitable.
- A strategy never reports profit; the portfolio module computes P&L from
  simulated or real fills.
- Backtest, paper and live P&L are labelled and never combined.
