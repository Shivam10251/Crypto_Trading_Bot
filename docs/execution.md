# Execution

Status: **paper execution implemented in Phase 8; gated by the risk engine
since Phase 9; exits added and reviewed in Phase 10.** Phase 17 builds the live
path and leaves it disabled.

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
partial fills. Both `OPEN` and `CLOSE` are wired: entries come from the
dispatcher, exits from `trading_bot.portfolio.closer` - see
[Exits](#exits). Market and IOC/FOK limit orders are supported. GTC is refused until
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

That question was open after Phase 8: the gross edge assumes the basis
converges, realised price P&L is
`signed_quantity x (entry basis - exit basis)`, and nothing opened a position
and closed it again. Phase 10 closes it - the mechanism, not the measurement:
positions now close and realised P&L is computed from actual fills, but no
result from a live run has been produced or calibrated yet.

## Exits

`trading_bot.portfolio.closer` is the only thing that closes a position, and
it uses the same adapter, book and latency the entry did. The pieces that
differ from an entry, and why:

| Entry | Exit |
| --- | --- |
| Order type from `execution.entry_order_type` | **Always MARKET.** An IOC limit can leave part of one leg unfilled - measured on 3 of 21 Phase 8 limit entries - and on an exit that failure mode *creates* the naked exposure the close was called to remove |
| `OrderIntent.OPEN` | `OrderIntent.CLOSE`, on the opposite side, for the position's remaining open quantity and no more |
| Reserves capacity in `PaperAccount` | Releases it, via `settle_exit`, at the entry notional of the quantity closed |
| Blocked by the kill switch | **Not blocked.** A halt stops new exposure; closing removes it. See [risk-management.md](risk-management.md#closing-a-position) |
| Recorded by the batched `ExecutionRecorder` | Recorded synchronously, in **one transaction** with the position rows it settles |

Both legs are submitted concurrently, exactly as an entry's are: a hedge that
unwinds in sequence is unhedged in between. Every exit is priced by walking the
current synchronised books for the exact residual quantity on the side that
would have to trade - a missing, unsynced or stale book prices nothing, and
there is no fallback to the last price seen.

A close that fills one leg and not the other leaves **real naked exposure**.
It is reported as such: a durable `ABNORMAL_EXECUTION` risk event, the kill
switch under `risk.pause_on_unhedged`, `unpaired_positions` on the next
portfolio snapshot, a DEGRADED health row, and an `UNPAIRED_RESIDUAL` close on
the next sweep.

Retries are idempotent within one claim: its execution-intent id is derived
from the attempt and claim counter, and the unique indexes turn a replay of
that claim into an update. A later terminal retry increments the counter and
uses a new identity. The position's exit accounting is recomputed from all of
its `CLOSE` fills rather than incremented, so applying the record twice is the
same as applying it once.

The paper close path derives size from freshly row-locked positions and risk
rechecks it after the claim. The database prevents recorded closed quantity
from exceeding opened quantity. A future live adapter must also send the
exchange-native reduce-only flag: a database constraint cannot undo an order
that an exchange has already filled.

`positions.is_shadow` keeps hypothetical probes out of account restoration,
portfolio reporting and the exit path entirely - a probe's exposure is
hypothetical, and closing it would place a real order.

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
