# Execution

Status: **paper execution implemented in Phase 8; gated by the risk engine
since Phase 9.** Phase 17 builds the live path and leaves it disabled.

## Adapter boundary

```
Strategy → Risk Engine → ExecutionAdapter
                          ├── PaperExecutionAdapter   (Phase 8)
                          └── LiveExecutionAdapter    (Phase 17, off)
```

```python
class ExecutionAdapter(Protocol):
    async def submit(self, request: OrderRequest) -> ExecutionResult: ...
    async def cancel(self, client_order_id: str) -> CancelAck: ...
    async def status(self, client_order_id: str) -> ExecutionResult | None: ...
```

`submit` returns the full `ExecutionResult` rather than an acknowledgement:
a paper order reaches a terminal state immediately, and splitting it into an
ack plus a later status poll would invent an asynchrony the simulator does
not have. The live adapter can fill the same type in as it learns more.

The strategy never chooses an adapter; the runtime injects one based on
configuration. The bounded dispatcher keeps adapter latency off the strategy
cadence and admits each opportunity episode once. Since Phase 9, the
dispatcher's workers call `RiskEngine.evaluate` before ever calling the
coordinator: a `REJECTED` or `PAUSED` verdict returns before the adapter is
reached, so nothing is submitted and no order row is written for it. An
`APPROVED` order carries the `risk_events.id` that authorised it on
`orders.risk_event_id`.

The coordinator then calls back into the risk engine one last time -
`ExecutionCoordinator.execute(..., admission=...)` - in the instant before
`adapter.submit`. A refusal there releases the reservation and returns
`None`: no order, no order row, and a durable risk event explaining it. The
dispatcher also drops queued work and asks the adapter to cancel anything
still open when the kill switch trips. See [risk-management.md](risk-management.md) for the
full set of checks, the kill switch, and the atomic reservation this shares
with the paper account below.

## Paper execution must not be optimistic

A signal is not a trade. The simulator models the ways execution actually
fails:

| Effect | Simulated as |
| --- | --- |
| Spread cost | Buy at ask, sell at bid — never at mid |
| Slippage | Walk the order book by size; depth-dependent |
| Latency | Delay between decision and fill; book may have moved |
| Partial fills | Fill only what the full locally known depth supports |
| Rejections | Order-type-aware price/size/notional filters, cash, margin, inventory, borrow and exposure |
| Cancellations | IOC/FOK remainder is cancelled immediately |
| Timeouts | No response within the configured window |
| Zero liquidity | Empty book on one side → no fill |

Supported adapter vocabulary is `BUY`, `SELL`, `OPEN`, `CLOSE`, `CANCEL`, and
partial fills. The service currently wires entries (`OPEN`) only; exits remain
Phase 10. Market and IOC/FOK limit orders are supported. GTC is refused until
trade prints and queue position can justify maker fills. Every fill stores its
fee rate, consumed levels, book sequence, and fill-time timestamp.

Execution is fail-closed when PostgreSQL is unavailable. The paper account
reserves cash and perpetual margin before concurrent leg submission, requires
spot inventory or an explicitly capped margin borrow, applies configured
gross-exposure limits, and restores open positions after restart. BNB-discounted
fees require a configured paper BNB balance. Since Phase 9 that reservation is
made by the risk engine, not the coordinator: `RiskEngine.evaluate` calls
`PaperAccount.reserve` (the coordinator calls it again with the same
`intent_id` and gets the same reservation back, unchanged), so risk policy and
account state share one lock-protected ledger instead of two that could
disagree. Releasing a reservation before submission leaves the intent
retryable, so every account limit is checked again; only an attempt that
reached settlement enters the completed-intent cache. After settlement, risk
also checks actual order, position and gross notionals because adverse fill
prices can cross a limit that expected notional passed. See
[risk-management.md](risk-management.md).

## What Phase 8 measured

The figures below are historical exploratory observations; their raw run
artifacts were not checked in and the simulator semantics have since changed.
They motivate tests, but are not reproducible acceptance evidence.

Three 110 s runs against the live book, 80 simulated orders. The headline is
in [development-phases.md](development-phases.md#phase-8--delivered); the two
findings that change how the system should be built:

- **The maker rate is unreachable by this strategy.** Zero maker fills out of
  42 limit orders. The strategy's target price is the VWAP of walking the
  book, so a limit there always crosses the spread. The 18.6 bps floor Phase 6
  computed assumed maker fills; the reachable floor is the 30 bps taker one.
- **An IOC limit can break the hedge.** Market entries left nothing
  naked across 19 attempts; limit entries left 3 of 21 naked, because each leg
  runs out of depth inside its own limit at a different point.

Realised slippage against the cost model's estimate averaged -0.014 bps on
market entries, so the entry half of the Phase 6 model is validated. The exit
half is not: nothing here closes a position yet.

## What Phase 8 can rely on

The remediation before it (see
[development-phases.md](development-phases.md#phase-75--correctness-remediation-delivered))
means the simulator receives inputs it can trust:

- a quantity valid on **both** venues - rounded down to a common step, inside
  each leg's `LOT_SIZE` and `MARKET_LOT_SIZE` bounds, above each minimum
  notional (including the perpetual minimum, which used to read as unknown)
- prices no stale quote, book or funding observation can produce
- no finite edge where the modelled unwind had no depth to price it
- a stored row carrying the exact book levels the decision used, so a paper
  fill can be compared against what the strategy believed

That question is still open after Phase 8: the gross edge assumes the basis
converges, realised price P&L is
`signed_quantity x (entry basis - exit basis)`, and nothing yet opens a
position and closes it again. `OrderIntent.CLOSE` exists and the simulator
will price one, but the service does not schedule exits. Phase 8 now records
open exposure in `positions`; `positions.is_shadow` keeps hypothetical probes
out of account restoration and portfolio reporting. Phase 10 supplies exit
policy and realised P&L.

## Live execution (Phase 17, disabled)

Enabling live trading requires **all** of:

1. `TB_PROFILE=production`
2. `TB_EXECUTION__LIVE_ENABLED=true`
3. `TB_EXECUTION__MODE=live`
4. a non-empty `TB_EXECUTION__LIVE_CONFIRMATION_PHRASE`
5. exchange API credentials present

Any other combination raises at startup. The `development` and `paper` profiles
reject live execution outright — the process refuses to boot rather than
trading unexpectedly. These guards are enforced in
`trading_bot.core.config` and covered by tests today, before any order-placing
code exists.

Additional requirements before live trading is considered:

- API keys without withdrawal permission, IP-allow-listed
- idempotency keys so a retry cannot duplicate an order
- order and position reconciliation against the exchange on startup
- an emergency shutdown that cancels open orders and flattens positions
