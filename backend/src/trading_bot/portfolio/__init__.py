"""Portfolio and P&L: what the positions are worth, and what closing them did.

Phase 10. The layers are kept apart on purpose, so the arithmetic can be
tested without a database and the policy without a market:

```
accounting.py   pure Decimal P&L per position and paired trade - no I/O
statistics.py   pure performance statistics over sets of trades and equity
exits.py        pure exit policy - when to stop holding, and why
valuation.py    executable exit prices from the live books
records.py      read models: a position and an attempt with their fills
store.py        durable reads, claims, and recomputation from fills
close_record.py one close leg as the order and fill rows that prove it
closer.py       orchestration: risk, both legs at once, one transaction
snapshots.py    valuing the book and writing the two snapshot tables
pnl_source.py   the realised P&L the Phase 9 risk limits were waiting for
service.py      the two loops, wired by the market-data service
```

Three rules run through all of it:

1. **Actual fills, never estimates.** Every figure comes from what executed.
   A strategy's expected price is kept only as slippage attribution.
2. **PAPER, LIVE and THEORETICAL never mix**, and ``is_shadow`` rows are
   excluded from every actionable total. Probes measure the venue, not the
   strategy.
3. **Unmeasured is not zero.** Funding and spot borrow are real costs nothing
   here can measure yet; they are NULL and named, and no total that omits
   them is called complete.
"""

from trading_bot.portfolio.accounting import (
    FillLot,
    PairedTrade,
    PositionPnl,
    TradeOutcome,
    position_pnl,
)
from trading_bot.portfolio.closer import CloseOutcome, PositionCloser
from trading_bot.portfolio.exits import BasisView, ExitDecision, ExitReason
from trading_bot.portfolio.pnl_source import PortfolioPnlSource, utc_day_start
from trading_bot.portfolio.records import AttemptRecord, LegRecord
from trading_bot.portfolio.service import PortfolioService
from trading_bot.portfolio.snapshots import PortfolioState, SnapshotWriter, Window, value_book
from trading_bot.portfolio.statistics import TradeStatistics, summarise_trades
from trading_bot.portfolio.store import PortfolioStore
from trading_bot.portfolio.valuation import ExecutableExit, MarkReader, price_exit

__all__ = [
    "AttemptRecord",
    "BasisView",
    "CloseOutcome",
    "ExecutableExit",
    "ExitDecision",
    "ExitReason",
    "FillLot",
    "LegRecord",
    "MarkReader",
    "PairedTrade",
    "PortfolioPnlSource",
    "PortfolioService",
    "PortfolioState",
    "PortfolioStore",
    "PositionCloser",
    "PositionPnl",
    "SnapshotWriter",
    "TradeOutcome",
    "TradeStatistics",
    "Window",
    "position_pnl",
    "price_exit",
    "summarise_trades",
    "utc_day_start",
    "value_book",
]
