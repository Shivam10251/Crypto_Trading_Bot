"""The portfolio subsystem's two loops, and the state they share.

```
exit loop      every exits.evaluate_interval_ms
                 recover interrupted closes -> evaluate policy -> close
snapshot loop  every portfolio.snapshot_interval_ms
                 value the book -> portfolio_snapshots
                                -> pnl_snapshots (portfolio, per strategy)
                                -> per-position rows for whatever just closed
                                -> refresh the risk engine's P&L view
```

They are separate loops because they answer to different clocks: an exit has
to be evaluated against a live book, and a snapshot is a periodic measurement
whose interval is also the return-sampling interval Sharpe and Sortino are
annualised from. Tying them together would force one of the two to lie about
its own cadence.

The service runs inside the market-data process, alongside the strategy and
execution, because it needs the same live feed the simulator fills against.
The API judges it the way it judges every other subsystem there: by the
durable rows it wrote.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from trading_bot.core.config import PortfolioConfig
from trading_bot.core.logging import get_logger
from trading_bot.portfolio.closer import PositionCloser
from trading_bot.portfolio.pnl_source import PortfolioPnlSource, utc_day_start
from trading_bot.portfolio.snapshots import (
    WINDOW_ALL,
    WINDOW_DAY,
    WINDOW_SESSION,
    PortfolioState,
    SnapshotWriter,
    Window,
    floor_to,
    value_book,
)
from trading_bot.portfolio.store import PortfolioStore
from trading_bot.portfolio.valuation import MarkReader

logger = get_logger(__name__)


def _utcnow() -> datetime:
    return datetime.now(UTC)


class PortfolioService:
    """Owns the exit loop, the snapshot loop and the counters the API reads."""

    def __init__(
        self,
        *,
        store: PortfolioStore,
        writer: SnapshotWriter,
        marks: MarkReader,
        closer: PositionCloser | None,
        pnl_source: PortfolioPnlSource | None,
        config: PortfolioConfig,
        venue: str,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._store = store
        self._writer = writer
        self._marks = marks
        self._closer = closer
        self._pnl_source = pnl_source
        self._config = config
        self._venue = venue
        self._clock = clock
        self._interval = timedelta(milliseconds=config.snapshot_interval_ms)
        self._session_start = floor_to(clock(), self._interval)
        # Per-position rows are emitted for positions that closed since the
        # previous snapshot, rather than tracked in a set that would grow for
        # the life of the process. A retry inside one interval re-emits the
        # same rows and the unique constraint upserts them.
        self._last_position_rows_at: datetime | None = None
        self.snapshots_written = 0
        self.pnl_rows_written = 0
        self.snapshot_failures = 0

    # --- loops ----------------------------------------------------------

    async def run_exits(self) -> None:
        if self._closer is None:
            return
        await self._closer.run()

    async def run_snapshots(self) -> None:
        while True:
            await asyncio.sleep(self._config.snapshot_interval_ms / 1000)
            try:
                await self.snapshot()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # one failed snapshot must not stop the loop
                self.snapshot_failures += 1
                logger.exception("portfolio.snapshot_failed", error=str(exc))

    # --- one snapshot ---------------------------------------------------

    async def snapshot(self) -> PortfolioState:
        """Value the book, persist it, and refresh what risk reads."""
        now = self._clock()
        captured_at = floor_to(now, self._interval)
        attempts = await self._store.live_attempts(self._venue)
        cash = await self._writer.cash()
        completed = await self._store.attempts_closed_between(self._venue, None, now)
        trades = [attempt.paired() for attempt in completed]
        realized = sum((trade.realized_pnl_usd for trade in trades), Decimal(0))
        state = value_book(
            attempts,
            self._marks,
            cash_usd=cash,
            realized_pnl_usd=realized,
            captured_at=captured_at,
        )
        await self._writer.write_portfolio(state)
        # The per-position mark is written after the snapshot, so a position's
        # own row can never claim a mark the portfolio total did not use.
        await self._store.record_marks(state.marks, now=captured_at)
        self.snapshots_written += 1
        self.pnl_rows_written += await self._write_pnl(state, trades, completed, now, captured_at)
        self._last_position_rows_at = captured_at
        if self._pnl_source is not None:
            # The risk engine's daily-loss and consecutive-loss view is
            # rebuilt from the same committed rows this snapshot just read.
            await self._pnl_source.refresh()
        logger.info(
            "portfolio.snapshot",
            captured_at=captured_at.isoformat(),
            equity=str(state.equity_usd) if state.equity_usd is not None else None,
            valuation=state.valuation_status.value,
            open_positions=state.open_positions,
            unvalued=state.unvalued_positions,
            unpaired=state.unpaired_positions,
            completed_trades=len(trades),
        )
        return state

    async def _write_pnl(
        self,
        state: PortfolioState,
        trades: list[Any],
        completed: list[Any],
        now: datetime,
        captured_at: datetime,
    ) -> int:
        windows = (
            Window(WINDOW_DAY, utc_day_start(now), now),
            Window(WINDOW_SESSION, self._session_start, now),
            Window(WINDOW_ALL, None, now),
        )
        rows: list[dict[str, Any]] = []
        for window in windows:
            curve = await self._writer.equity_curve(window)
            inside = [
                trade
                for trade in trades
                if trade.closed_at is not None
                and (window.start is None or trade.closed_at >= window.start)
            ]
            rows.append(
                self._writer.pnl_row(
                    window=window,
                    captured_at=captured_at,
                    trades=inside,
                    curve=curve,
                    unrealized_pnl_usd=state.unrealized_pnl_usd,
                    equity_usd=state.equity_usd,
                    additional_unmeasured=state.unmeasured_pnl,
                )
            )
            strategies = {trade.strategy for trade in inside} | set(
                state.strategy_unrealized_pnl_usd
            )
            for strategy in sorted(strategies):
                rows.append(
                    self._writer.pnl_row(
                        window=window,
                        captured_at=captured_at,
                        trades=[t for t in inside if t.strategy == strategy],
                        # A strategy has no equity curve of its own: equity is
                        # an account-level figure, so the ratios that need one
                        # stay NULL here rather than borrowing the portfolio's.
                        curve=(),
                        strategy=strategy,
                        unrealized_pnl_usd=state.strategy_unrealized_pnl_usd.get(
                            strategy, Decimal(0)
                        ),
                        additional_unmeasured=state.strategy_unmeasured_pnl.get(strategy, ()),
                    )
                )
        rows.extend(self._position_rows(completed))
        return await self._writer.write_pnl(rows)

    def _position_rows(self, completed: list[Any]) -> list[dict[str, Any]]:
        """One all-time row per position, written when it closes.

        ``captured_at`` is the position's own ``closed_at`` rather than this
        cycle's instant, which is what makes the row idempotent: a realized
        position's result is final, so re-deriving it lands on the row already
        there instead of adding one per snapshot. Only positions closed since
        the previous snapshot are considered, so the work per cycle is bounded
        by what actually happened rather than by the whole history.
        """
        since = self._last_position_rows_at
        rows: list[dict[str, Any]] = []
        for attempt in completed:
            trade = attempt.paired()
            if not trade.is_complete or trade.closed_at is None:
                continue
            for leg, record in zip(trade.legs, attempt.legs, strict=True):
                if record.closed_at is None or (since is not None and record.closed_at < since):
                    continue
                rows.append(
                    self._writer.pnl_row(
                        window=Window(WINDOW_ALL, record.opened_at, record.closed_at),
                        captured_at=record.closed_at,
                        # A single leg is not a trade: its statistics would
                        # score half of a hedged pair. The row carries the
                        # leg's money, and no trade counts at all.
                        trades=(),
                        curve=(),
                        position_id=record.position_id,
                    )
                    | {
                        "realized_pnl_usd": leg.realized_pnl_usd,
                        "fees_usd": leg.fees_on_closed_usd,
                        "slippage_usd": leg.slippage_usd,
                        "unmeasured_pnl": list(leg.unmeasured) or None,
                        "strategy": record.strategy,
                        "scope_key": f"position:{record.position_id}",
                    }
                )
        return rows
