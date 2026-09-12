"""Execution: turning a validated signal into fills, or into the reason there were none.

Paper today (Phase 8), live behind a flag much later (Phase 17). The strategy
never chooses an adapter and never imports this package; the runtime injects
one from configuration, which is the whole difference between the two modes.

Nothing here is random. Every way an order fails to become a complete fill -
a venue filter, depth that ran out, a book that went stale, a resting order
the market never came back to - is derived from something observed, because a
simulator whose disappointments come from a coin flip measures its own seed.
"""

from trading_bot.execution.account import PaperAccount
from trading_bot.execution.base import ExecutionAdapter, MarketFeed, SpecSource
from trading_bot.execution.coordinator import (
    ExecutionAttempt,
    ExecutionCoordinator,
    LegOutcome,
)
from trading_bot.execution.dispatcher import ExecutionDispatcher
from trading_bot.execution.models import (
    TERMINAL_STATUSES,
    CancelAck,
    ExecutionResult,
    OrderAck,
    OrderIntent,
    OrderRequest,
    RejectionCode,
    SimulatedFill,
)
from trading_bot.execution.paper import PaperExecutionAdapter
from trading_bot.execution.recorder import (
    MAX_PENDING_ATTEMPTS,
    ExecutionRecorder,
    fill_rows,
    order_row,
    summarise,
)

__all__ = [
    "MAX_PENDING_ATTEMPTS",
    "TERMINAL_STATUSES",
    "CancelAck",
    "ExecutionAdapter",
    "ExecutionAttempt",
    "ExecutionCoordinator",
    "ExecutionDispatcher",
    "ExecutionRecorder",
    "ExecutionResult",
    "LegOutcome",
    "MarketFeed",
    "OrderAck",
    "OrderIntent",
    "OrderRequest",
    "PaperAccount",
    "PaperExecutionAdapter",
    "RejectionCode",
    "SimulatedFill",
    "SpecSource",
    "fill_rows",
    "order_row",
    "summarise",
]
