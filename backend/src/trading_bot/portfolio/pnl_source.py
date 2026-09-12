"""Realised P&L for the risk engine, read from committed rows only.

This is what Phase 9 deferred. `max_daily_loss_usd` and
`max_consecutive_losses` were built, tested and then honestly reported as
unenforced because there was no trustworthy realised P&L to gate on; this
module supplies one, and the two limits start operating.

Four decisions that decide whether the numbers mean anything:

**Committed rows, never in-flight state.** Every figure comes from
``positions`` rows whose close is durable. A position still ``CLOSING``, a
fill not yet written, a close in progress in this process - none of them
count. A limit that could be moved by work that later rolled back would stop
trading for a loss that never happened, or fail to stop it for one that did.

**The trade is the pair, not the leg.** A basis attempt's spot leg losing what
its perpetual leg made is the *intended* outcome. Counting legs would report
a 50% win rate on a book that never made or lost anything, and
``max_consecutive_losses`` would fire on the losing half of every hedged
trade. Both limits count completed paired attempts.

**A day is a UTC day.** The daily window is ``[00:00 UTC today, now)``, and an
attempt belongs to the day its **last** leg closed - the day the trade
finished, since that is when its result existed. Documented rather than
inferred because "today" is otherwise a property of whichever machine asked.

**Unavailable is not zero.** A database that cannot be read returns ``None``,
which is exactly what ``NullPnlSource`` returned before this existed, so
Phase 9's fail-closed behaviour is preserved unchanged: the configured
``daily_loss_policy`` / ``consecutive_loss_policy`` decides, and neither
limit is ever evaluated against a fabricated zero.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from trading_bot.core.logging import get_logger
from trading_bot.portfolio.accounting import TradeOutcome
from trading_bot.portfolio.records import AttemptRecord
from trading_bot.portfolio.store import PortfolioStore
from trading_bot.risk.models import RealizedPnl

logger = get_logger(__name__)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def utc_day_start(now: datetime) -> datetime:
    """Midnight UTC of ``now``'s day - the daily-loss window's boundary."""
    moment = now.astimezone(UTC)
    return moment.replace(hour=0, minute=0, second=0, microsecond=0)


class PortfolioPnlSource:
    """``PnlSource`` backed by durable paired-trade results.

    Cached for ``refresh_interval`` so evaluating a signal does not cost a
    query each time, and refreshable on demand so a trade that just closed is
    visible to the next decision rather than to the one after the interval.
    The cache holds only committed rows, so it can be stale but never wrong
    about work that has not landed.
    """

    def __init__(
        self,
        store: PortfolioStore,
        *,
        venue: str,
        streak_limit: int = 100,
        refresh_interval: timedelta = timedelta(seconds=1),
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._store = store
        self._venue = venue
        self._interval = refresh_interval
        self._streak_limit = max(1, streak_limit)
        self._clock = clock or _utcnow
        self._read_at: datetime | None = None
        self._today: RealizedPnl | None = None
        self._streak: int | None = None
        self._refresh_lock = asyncio.Lock()
        self.read_failures = 0

    def _now(self) -> datetime:
        return self._clock()

    async def realized_pnl_today_usd(self) -> RealizedPnl | None:
        await self._ensure_fresh()
        return self._today

    async def consecutive_losses(self) -> int | None:
        await self._ensure_fresh()
        return self._streak

    async def refresh(self) -> None:
        """Re-read now. Called after a close, and on the snapshot cadence."""
        async with self._refresh_lock:
            await self._refresh_unlocked()

    async def _refresh_unlocked(self) -> None:
        now = self._now()
        try:
            today = await self._store.attempts_closed_between(self._venue, utc_day_start(now), now)
            latest = await self._store.latest_closed_attempts(
                self._venue, now, limit=self._streak_limit
            )
        except Exception as exc:
            # Keep nothing: a stale view is worse than an unavailable one for
            # a limit whose whole job is to notice a loss. The configured
            # policy then decides, exactly as it did with no source at all.
            self.read_failures += 1
            self._today = None
            self._streak = None
            self._read_at = now
            logger.error("portfolio.pnl_source_unreadable", error=str(exc))
            return
        self._today = _today(today, now)
        self._streak = _consecutive_losses(latest)
        self._read_at = now

    async def _ensure_fresh(self) -> None:
        if self._read_at is None or self._now() - self._read_at >= self._interval:
            async with self._refresh_lock:
                # Another worker may have refreshed while this one waited.
                if self._read_at is None or self._now() - self._read_at >= self._interval:
                    await self._refresh_unlocked()


def _today(completed: list[AttemptRecord], now: datetime) -> RealizedPnl:
    """Net realised P&L over the attempts that completed today, UTC."""
    start = utc_day_start(now)
    net = Decimal(0)
    trades = 0
    unmeasured: list[str] = []
    for attempt in completed:
        trade = attempt.paired()
        if trade.closed_at is None or trade.closed_at < start or not trade.is_complete:
            continue
        net += trade.realized_pnl_usd
        trades += 1
        for component in trade.unmeasured:
            if component not in unmeasured:
                unmeasured.append(component)
    return RealizedPnl(
        net_usd=net,
        trades=trades,
        unmeasured=tuple(unmeasured),
        window_start=start,
        as_of=now,
    )


def _consecutive_losses(completed: list[AttemptRecord]) -> int:
    """Losing paired trades at the end of the record, most recent first.

    A breakeven trade breaks the run: the limit exists to notice a strategy
    that has stopped working, and a trade that lost nothing is not evidence
    of that.
    """
    trades = [attempt.paired() for attempt in completed]
    finished = [trade for trade in trades if trade.is_complete and trade.closed_at is not None]
    finished.sort(key=lambda trade: trade.closed_at, reverse=True)  # type: ignore[arg-type,return-value]
    streak = 0
    for trade in finished:
        if trade.outcome is not TradeOutcome.LOSS:
            break
        streak += 1
    return streak
