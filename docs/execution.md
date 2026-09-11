# Execution

Status: **not implemented.** Phase 8 builds paper execution; Phase 17 builds
the live path and leaves it disabled.

## Adapter boundary

```
Strategy → Risk Engine → ExecutionAdapter
                          ├── PaperExecutionAdapter   (Phase 8)
                          └── LiveExecutionAdapter    (Phase 17, off)
```

```python
class ExecutionAdapter(Protocol):
    async def submit(self, order: OrderRequest) -> OrderAck: ...
    async def cancel(self, order_id: OrderId) -> CancelAck: ...
    async def status(self, order_id: OrderId) -> OrderStatus: ...
```

The strategy never chooses an adapter; the runtime injects one based on
configuration. Swapping adapters is the only difference between paper and live.

## Paper execution must not be optimistic

A signal is not a trade. The simulator models the ways execution actually
fails:

| Effect | Simulated as |
| --- | --- |
| Spread cost | Buy at ask, sell at bid — never at mid |
| Slippage | Walk the order book by size; depth-dependent |
| Latency | Delay between decision and fill; book may have moved |
| Partial fills | Fill only what the visible depth supports |
| Rejections | Price/size filters, insufficient margin |
| Cancellations | Orders that expire before filling |
| Timeouts | No response within the configured window |
| Zero liquidity | Empty book on one side → no fill |

Supported operations: `BUY`, `SELL`, `OPEN`, `CLOSE`, `CANCEL`, and partial
fills. Every simulated order records the book snapshot it was priced against,
so any paper fill can be re-derived later.

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
