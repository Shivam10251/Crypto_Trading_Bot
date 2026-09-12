"""The execution boundary.

```
Strategy -> (Risk Engine, Phase 9) -> ExecutionAdapter
                                       |- PaperExecutionAdapter   (Phase 8)
                                       '- LiveExecutionAdapter    (Phase 17, off)
```

The strategy never chooses an adapter; the runtime injects one from
configuration. That is the whole difference between paper and live, and it is
why this protocol is narrow: three methods, all in the vocabulary of
``execution.models``, none of them venue-specific. An adapter that needed a
fifth method for its venue would be leaking the venue upward.

``MarketFeed`` is the other half of the boundary, and it exists so the paper
adapter can read the book **at fill time** rather than being handed the book
the strategy decided on. Those are different books - that gap is the latency -
and a simulator given the decision's book cannot model the market moving away
from an order, which is the most common way a real fill disappoints.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

from trading_bot.exchange.models import MarketRef, MarketSpec
from trading_bot.execution.models import CancelAck, ExecutionResult, OrderRequest
from trading_bot.marketdata.models import MarketSnapshot

if TYPE_CHECKING:
    from trading_bot.execution.account import AccountRejection


class MarketFeed(Protocol):
    """The read API a simulator needs: one market's state, right now.

    ``MarketDataEngine`` satisfies this already. Stated as a protocol so the
    simulator is testable without a socket, and so nothing in execution
    imports the engine.
    """

    def snapshot(self, ref: MarketRef) -> MarketSnapshot: ...


class SpecSource(Protocol):
    """Instrument reference data: the filters an order has to satisfy."""

    def __call__(self, ref: MarketRef) -> MarketSpec | None: ...


class AdmissionCheck(Protocol):
    """The risk engine's last word, called in the instant before submission.

    Stated as a protocol so the coordinator never imports the risk engine:
    execution knows only that something may refuse admission, and what a
    refusal looks like. Returning ``None`` admits the order.
    """

    async def __call__(self) -> AccountRejection | None: ...


@runtime_checkable
class ExecutionAdapter(Protocol):
    """Somewhere an order can be sent. Paper today, live behind a flag."""

    async def submit(self, request: OrderRequest) -> ExecutionResult:
        """Place one order and report everything that became of it."""
        ...

    async def cancel(self, client_order_id: str) -> CancelAck:
        """Withdraw a resting order. Fails if it already reached a terminal state."""
        ...

    async def status(self, client_order_id: str) -> ExecutionResult | None:
        """The latest state of an order, or ``None`` if this adapter never saw it."""
        ...
