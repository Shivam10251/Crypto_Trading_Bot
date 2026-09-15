"""The figures a P&L row needs, from a batch of history or folded in as it happens.

``PortfolioService`` used to re-read every completed trade and the whole
equity curve on every snapshot, which Phase 10 documented would not scale. It
was measured in Phase 11 (``scripts/bench_snapshot_scaling.py``): one snapshot
cost 49 ms with 250 closed trades, 389 ms with 4,000, and 226 ms with 43,200
prior snapshots - linear per snapshot, so quadratic over a run. A 30-day
backtest writes 43,200 snapshots.

So the same figures are available two ways, and they are the same figures:

- ``trade_figures`` / ``curve_figures`` compute them from a full history, as
  the paper service always has;
- ``TradeTally`` / ``CurveTally`` fold one trade or one equity point in at a
  time, in constant work and memory per update.

**Exactly equal, except two floats.** Every Decimal figure - realised P&L,
fees, slippage, funding, drawdown, trade counts - is identical either way, and
so are total return, win rate and profit factor. Sharpe and Sortino agree only
to floating-point rounding (tested to 1e-9 relative): Python's ``sum`` of
floats is compensated while a running sum is not, and Sharpe's deviation is
taken from running sums of squares rather than from a final mean.

**Only for a single writer.** Folding assumes every trade that closed before
the last snapshot was already committed when that snapshot read. A replay
guarantees that; the concurrent paper service does not, so it keeps reading
history.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from trading_bot.portfolio.accounting import FUNDING, PairedTrade
from trading_bot.portfolio.statistics import (
    TradeStatistics,
    max_drawdown,
    sample_returns,
    sharpe_ratio,
    sortino_ratio,
    summarise_trades,
    total_return_pct,
)

ZERO = Decimal(0)
#: ``statistics.sample_returns``' own spacing tolerance.
SPACING_TOLERANCE = 0.25


@dataclass(frozen=True, slots=True)
class TradeFigures:
    statistics: TradeStatistics
    fees_usd: Decimal
    slippage_usd: Decimal
    unmeasured: tuple[str, ...]
    #: Total attributed funding, ``None`` unless every trade measured it.
    funding_usd: Decimal | None


@dataclass(frozen=True, slots=True)
class CurveFigures:
    max_drawdown_usd: Decimal | None
    total_return_pct: float | None
    return_observations: int
    sharpe_ratio: float | None
    sortino_ratio: float | None


def trade_figures(trades: Sequence[PairedTrade]) -> TradeFigures:
    unmeasured: list[str] = []
    for trade in trades:
        for component in trade.unmeasured:
            if component not in unmeasured:
                unmeasured.append(component)
    measured = bool(trades) and all(FUNDING not in trade.unmeasured for trade in trades)
    return TradeFigures(
        statistics=summarise_trades([trade.realized_pnl_usd for trade in trades]),
        fees_usd=sum((trade.fees_usd for trade in trades), ZERO),
        slippage_usd=sum((trade.slippage_usd for trade in trades), ZERO),
        unmeasured=tuple(unmeasured),
        funding_usd=(
            sum(
                (leg.funding_pnl_usd or ZERO for trade in trades for leg in trade.legs),
                ZERO,
            )
            if measured
            else None
        ),
    )


def curve_figures(
    curve: Sequence[tuple[datetime, Decimal]],
    *,
    interval: timedelta,
    risk_free_per_period: float,
    minimum: int,
) -> CurveFigures:
    equity = [value for _, value in curve]
    sample = sample_returns(list(curve), interval=interval)
    return CurveFigures(
        max_drawdown_usd=max_drawdown(equity),
        total_return_pct=total_return_pct(equity),
        return_observations=len(sample.returns) if sample is not None else 0,
        sharpe_ratio=(
            sharpe_ratio(sample, risk_free_per_period=risk_free_per_period, minimum=minimum)
            if sample is not None
            else None
        ),
        sortino_ratio=(
            sortino_ratio(sample, risk_free_per_period=risk_free_per_period, minimum=minimum)
            if sample is not None
            else None
        ),
    )


@dataclass(slots=True)
class TradeTally:
    """Completed paired trades, folded in one at a time."""

    count: int = 0
    wins: int = 0
    losses: int = 0
    gross_profit: Decimal = ZERO
    gross_loss: Decimal = ZERO
    net: Decimal = ZERO
    fees: Decimal = ZERO
    slippage: Decimal = ZERO
    unmeasured: list[str] = field(default_factory=list)
    funding: Decimal = ZERO
    funding_complete: bool = True

    def add(self, trade: PairedTrade) -> None:
        value = trade.realized_pnl_usd
        self.count += 1
        self.net += value
        if value > 0:
            self.wins += 1
            self.gross_profit += value
        elif value < 0:
            self.losses += 1
            self.gross_loss += value
        self.fees += trade.fees_usd
        self.slippage += trade.slippage_usd
        for component in trade.unmeasured:
            if component not in self.unmeasured:
                self.unmeasured.append(component)
        if FUNDING in trade.unmeasured:
            self.funding_complete = False
        else:
            self.funding += sum((leg.funding_pnl_usd or ZERO for leg in trade.legs), ZERO)

    def statistics(self) -> TradeStatistics:
        count = self.count
        average = self.net / count if count else None
        return TradeStatistics(
            trade_count=count,
            winning_trades=self.wins,
            losing_trades=self.losses,
            breakeven_trades=count - self.wins - self.losses,
            gross_profit_usd=self.gross_profit,
            gross_loss_usd=self.gross_loss,
            net_pnl_usd=self.net,
            average_trade_usd=average,
            average_win_usd=self.gross_profit / self.wins if self.wins else None,
            average_loss_usd=self.gross_loss / self.losses if self.losses else None,
            win_rate=self.wins / count if count else None,
            profit_factor=(
                float(self.gross_profit / -self.gross_loss) if self.gross_loss < 0 else None
            ),
            expectancy_usd=average,
        )

    def figures(self) -> TradeFigures:
        return TradeFigures(
            statistics=self.statistics(),
            fees_usd=self.fees,
            slippage_usd=self.slippage,
            unmeasured=tuple(self.unmeasured),
            funding_usd=self.funding if self.count and self.funding_complete else None,
        )


@dataclass(slots=True)
class CurveTally:
    """An equity curve, folded in one point at a time.

    The newest point is held apart from everything before it, because a
    snapshot retried inside its interval upserts the same ``captured_at`` - and
    the curve read back from the database then holds the new value, not both.
    """

    interval: timedelta
    risk_free_per_period: float
    first: Decimal | None = None
    previous: tuple[datetime, Decimal] | None = None
    last: tuple[datetime, Decimal] | None = None
    points: int = 0
    # Over every point before ``last``.
    peak: Decimal | None = None
    worst_drawdown: Decimal = ZERO
    # Over every return before the one ending at ``last``.
    returns: int = 0
    excess_sum: float = 0.0
    square_sum: float = 0.0
    downside_square_sum: float = 0.0
    has_downside: bool = False
    irregular: bool = False

    def add(self, at: datetime, equity: Decimal) -> None:
        if self.last is not None and at == self.last[0]:
            self.last = (at, equity)
            if self.points == 1:
                self.first = equity
            return
        if self.last is not None:
            self._fold(self.last)
        self.previous, self.last = self.last, (at, equity)
        self.points += 1
        if self.first is None:
            self.first = equity

    def _fold(self, point: tuple[datetime, Decimal]) -> None:
        """Commit ``point`` - the old ``last`` - and the return that ends at it."""
        _, value = point
        if self.peak is not None:
            self.worst_drawdown = max(self.worst_drawdown, self.peak - value)
        self.peak = value if self.peak is None else max(self.peak, value)
        if self.previous is not None:
            self._fold_return(self.previous, point)

    def _fold_return(
        self, earlier: tuple[datetime, Decimal], later: tuple[datetime, Decimal]
    ) -> None:
        excess = self._excess(earlier, later)
        if excess is None:
            self.irregular = True
            return
        self.returns += 1
        self.excess_sum += excess
        self.square_sum += excess * excess
        if excess < 0:
            self.has_downside = True
            self.downside_square_sum += excess**2

    def _excess(
        self, earlier: tuple[datetime, Decimal], later: tuple[datetime, Decimal]
    ) -> float | None:
        expected = self.interval.total_seconds()
        gap = (later[0] - earlier[0]).total_seconds()
        if gap <= 0 or abs(gap - expected) > expected * SPACING_TOLERANCE or earlier[1] <= 0:
            return None
        return float((later[1] - earlier[1]) / earlier[1]) - self.risk_free_per_period

    def figures(self, *, minimum: int) -> CurveFigures:
        if self.last is None or self.first is None:
            return CurveFigures(None, None, 0, None, None)
        last_value = self.last[1]
        drawdown: Decimal | None = None
        if self.points >= 2:
            peak = self.peak if self.peak is not None else last_value
            drawdown = max(self.worst_drawdown, peak - last_value)
        total = (
            float((last_value - self.first) / self.first * 100)
            if self.points >= 2 and self.first > 0
            else None
        )
        # The return ending at the newest point, applied without committing it.
        returns, excess_sum, square_sum = self.returns, self.excess_sum, self.square_sum
        downside_sum, has_downside, irregular = (
            self.downside_square_sum,
            self.has_downside,
            self.irregular,
        )
        if self.previous is not None:
            excess = self._excess(self.previous, self.last)
            if excess is None:
                irregular = True
            else:
                returns += 1
                excess_sum += excess
                square_sum += excess * excess
                if excess < 0:
                    has_downside = True
                    downside_sum += excess**2
        if irregular or returns == 0 or self.interval.total_seconds() <= 0:
            return CurveFigures(drawdown, total, 0, None, None)
        annualise = math.sqrt(timedelta(days=365).total_seconds() / self.interval.total_seconds())
        sharpe: float | None = None
        sortino: float | None = None
        if returns >= minimum:
            mean = excess_sum / returns
            variance = max(0.0, (square_sum - returns * mean * mean) / (returns - 1))
            deviation = math.sqrt(variance)
            if deviation > 0:
                sharpe = mean / deviation * annualise
            if has_downside:
                downside = math.sqrt(downside_sum / returns)
                if downside > 0:
                    sortino = mean / downside * annualise
        return CurveFigures(drawdown, total, returns, sharpe, sortino)


@dataclass(slots=True)
class WindowTally:
    """One reporting window's trades, per-strategy trades and equity curve."""

    start: datetime | None
    curve: CurveTally
    trades: TradeTally = field(default_factory=TradeTally)
    strategies: dict[str, TradeTally] = field(default_factory=dict)

    def add_trade(self, trade: PairedTrade) -> None:
        if trade.closed_at is None or (self.start is not None and trade.closed_at < self.start):
            return
        self.trades.add(trade)
        self.strategies.setdefault(trade.strategy, TradeTally()).add(trade)

    def add_point(self, at: datetime, equity: Decimal) -> None:
        if self.start is None or at >= self.start:
            self.curve.add(at, equity)
