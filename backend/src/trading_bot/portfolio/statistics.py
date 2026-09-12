"""Performance statistics over completed trades, and over the equity curve.

Split from ``accounting`` because it answers a different question: that module
says what one position or one paired trade did, this one says what a *set* of
them did, and what the account's equity did while they were on.

Two rules, both about refusing to produce a number:

1. **A ratio whose inputs do not exist is ``None``, not a placeholder.** A win
   rate over zero trades is not 0.0; a profit factor with no losing trade is
   not infinity; a drawdown over one observation is not zero.
2. **Sharpe and Sortino need a stated, regular sampling interval.** They are
   annualised by ``sqrt(periods per year)`` for the interval the sample was
   actually taken at, and a series whose spacing varied is refused rather than
   annualised on a guess. Annualising irregular event-level returns as though
   they were daily is the most common way a Sharpe ratio comes to describe
   nothing at all.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from itertools import pairwise

ZERO = Decimal(0)


@dataclass(frozen=True, slots=True)
class TradeStatistics:
    """Per-trade performance over a set of **completed** paired trades.

    Every ratio is ``None`` rather than a placeholder when its inputs do not
    exist: a win rate over zero trades is not 0.0, and a profit factor with no
    losing trade is not infinity.
    """

    trade_count: int
    winning_trades: int
    losing_trades: int
    breakeven_trades: int
    gross_profit_usd: Decimal
    gross_loss_usd: Decimal
    net_pnl_usd: Decimal
    average_trade_usd: Decimal | None
    average_win_usd: Decimal | None
    average_loss_usd: Decimal | None
    win_rate: float | None
    profit_factor: float | None
    expectancy_usd: Decimal | None


def summarise_trades(results: Sequence[Decimal]) -> TradeStatistics:
    """Statistics over completed paired trades' net results.

    ``expectancy`` is the mean net result per trade, which is identically
    ``win_rate * average_win + loss_rate * average_loss`` - stated as the mean
    so a breakeven trade is counted in the denominator rather than quietly
    dropped by a formula that only knows about wins and losses.
    """
    wins = [value for value in results if value > 0]
    losses = [value for value in results if value < 0]
    breakeven = len(results) - len(wins) - len(losses)
    gross_profit = sum(wins, ZERO)
    gross_loss = sum(losses, ZERO)  # negative or zero
    net = sum(results, ZERO)
    count = len(results)
    average = net / count if count else None
    return TradeStatistics(
        trade_count=count,
        winning_trades=len(wins),
        losing_trades=len(losses),
        breakeven_trades=breakeven,
        gross_profit_usd=gross_profit,
        gross_loss_usd=gross_loss,
        net_pnl_usd=net,
        average_trade_usd=average,
        average_win_usd=gross_profit / len(wins) if wins else None,
        average_loss_usd=gross_loss / len(losses) if losses else None,
        win_rate=len(wins) / count if count else None,
        # Undefined without a loss to divide by. Reporting "infinity" - or a
        # large number - would read as a measured edge rather than as one
        # untested by any losing trade.
        profit_factor=float(gross_profit / -gross_loss) if gross_loss < 0 else None,
        expectancy_usd=average,
    )


def max_drawdown(equity: Sequence[Decimal]) -> Decimal | None:
    """Largest peak-to-trough decline of an equity curve, as a positive number.

    ``None`` for fewer than two points: one observation cannot describe a
    decline, and zero would claim it never fell.
    """
    if len(equity) < 2:
        return None
    peak = equity[0]
    worst = ZERO
    for value in equity[1:]:
        peak = max(peak, value)
        worst = max(worst, peak - value)
    return worst


def total_return_pct(equity: Sequence[Decimal]) -> float | None:
    """Percentage change between the first and last equity point."""
    if len(equity) < 2 or equity[0] <= 0:
        return None
    return float((equity[-1] - equity[0]) / equity[0] * 100)


@dataclass(frozen=True, slots=True)
class ReturnSample:
    """Regularly spaced simple returns, and the interval they were taken at.

    Both fields matter: annualising a return series needs its sampling
    interval, and a series whose spacing varied is not a series at that
    interval at all.
    """

    returns: tuple[float, ...]
    interval: timedelta

    @property
    def periods_per_year(self) -> float:
        return timedelta(days=365).total_seconds() / self.interval.total_seconds()


def sample_returns(
    points: Sequence[tuple[datetime, Decimal]],
    *,
    interval: timedelta,
    tolerance: float = 0.25,
) -> ReturnSample | None:
    """Simple returns between consecutive points, if they are regular enough.

    Event-level P&L observations are not a daily return series, and
    annualising them as though they were is the most common way a Sharpe
    ratio comes to describe nothing. So the spacing is checked: every gap must
    be within ``tolerance`` of ``interval``, and a series that is not is
    refused (``None``) rather than annualised on a guess.
    """
    if len(points) < 2 or interval.total_seconds() <= 0:
        return None
    expected = interval.total_seconds()
    returns: list[float] = []
    for (earlier_at, earlier), (later_at, later) in pairwise(points):
        gap = (later_at - earlier_at).total_seconds()
        if gap <= 0 or abs(gap - expected) > expected * tolerance:
            return None
        if earlier <= 0:
            return None
        returns.append(float((later - earlier) / earlier))
    return ReturnSample(returns=tuple(returns), interval=interval)


def sharpe_ratio(
    sample: ReturnSample, *, risk_free_per_period: float = 0.0, minimum: int = 30
) -> float | None:
    """Annualised Sharpe, or ``None`` when the sample cannot support one.

    Requires ``minimum`` observations and a non-zero standard deviation.
    Annualised by ``sqrt(periods per year)`` for the sample's own interval,
    which is why ``ReturnSample`` carries it.
    """
    excess = [value - risk_free_per_period for value in sample.returns]
    if len(excess) < minimum:
        return None
    mean = sum(excess) / len(excess)
    variance = sum((value - mean) ** 2 for value in excess) / (len(excess) - 1)
    deviation = math.sqrt(variance)
    if deviation <= 0:
        return None
    return mean / deviation * math.sqrt(sample.periods_per_year)


def sortino_ratio(
    sample: ReturnSample, *, risk_free_per_period: float = 0.0, minimum: int = 30
) -> float | None:
    """Annualised Sortino: the same, against downside deviation only.

    ``None`` when nothing in the sample fell below the target - no downside
    observations means no downside deviation to divide by, and a very large
    number there would read as an edge rather than as an untested one.
    """
    excess = [value - risk_free_per_period for value in sample.returns]
    if len(excess) < minimum:
        return None
    mean = sum(excess) / len(excess)
    downside = [value for value in excess if value < 0]
    if not downside:
        return None
    deviation = math.sqrt(sum(value**2 for value in downside) / len(excess))
    if deviation <= 0:
        return None
    return mean / deviation * math.sqrt(sample.periods_per_year)
