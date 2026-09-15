"""Folding history in must give the numbers reading it would have."""

from __future__ import annotations

import math
import random
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from trading_bot.db.models.enums import Side
from trading_bot.portfolio.accounting import FillLot, PairedTrade, position_pnl
from trading_bot.portfolio.incremental import (
    CurveTally,
    TradeTally,
    curve_figures,
    trade_figures,
)

T0 = datetime(2026, 9, 1, tzinfo=UTC)
MINUTE = timedelta(minutes=1)


def trade(index: int, result: str, *, funding: Decimal | None) -> PairedTrade:
    at = T0 + timedelta(minutes=index)
    entry = FillLot(Decimal(100), Decimal(1), Decimal("0.1"), at, Decimal("99.9"))
    exit_ = FillLot(
        Decimal(100) + Decimal(result), Decimal(1), Decimal("0.1"), at + MINUTE, Decimal(100)
    )
    long = position_pnl(side=Side.BUY, entries=[entry], exits=[exit_], funding_pnl_usd=Decimal(0))
    short_entry = FillLot(Decimal(100), Decimal(1), Decimal(0), at)
    short_exit = FillLot(Decimal(100), Decimal(1), Decimal(0), at + MINUTE)
    short = position_pnl(
        side=Side.SELL, entries=[short_entry], exits=[short_exit], funding_pnl_usd=funding
    )
    return PairedTrade(f"a{index}", "s", (long, short), at, at + MINUTE)


@pytest.mark.parametrize("seed", range(5))
def test_trade_tally_equals_the_batch(seed: int) -> None:
    rng = random.Random(seed)  # noqa: S311 - reproducible test data
    trades = [
        trade(
            index,
            str(Decimal(rng.randint(-500, 500)) / 100),
            funding=None if rng.random() < 0.2 else Decimal(rng.randint(-9, 9)) / 1000,
        )
        for index in range(rng.randint(0, 40))
    ]
    tally = TradeTally()
    for item in trades:
        tally.add(item)
    assert tally.figures() == trade_figures(trades)


def test_funding_totals_only_when_every_trade_measured_it() -> None:
    measured = [trade(0, "1", funding=Decimal("0.5")), trade(1, "1", funding=Decimal("-0.2"))]
    assert trade_figures(measured).funding_usd == Decimal("0.3")
    assert trade_figures([*measured, trade(2, "1", funding=None)]).funding_usd is None
    assert trade_figures([]).funding_usd is None


def _compare(curve: list[tuple[datetime, Decimal]], *, risk_free: float = 0.0) -> None:
    tally = CurveTally(interval=MINUTE, risk_free_per_period=risk_free)
    for point in curve:
        tally.add(*point)
    folded = tally.figures(minimum=3)
    batch = curve_figures(curve, interval=MINUTE, risk_free_per_period=risk_free, minimum=3)
    assert folded.max_drawdown_usd == batch.max_drawdown_usd
    assert folded.total_return_pct == batch.total_return_pct
    assert folded.return_observations == batch.return_observations
    for folded_ratio, batch_ratio in (
        (folded.sharpe_ratio, batch.sharpe_ratio),
        (folded.sortino_ratio, batch.sortino_ratio),
    ):
        if batch_ratio is None:
            assert folded_ratio is None
        else:
            assert folded_ratio is not None
            assert math.isclose(folded_ratio, batch_ratio, rel_tol=1e-9)


@pytest.mark.parametrize("seed", range(10))
def test_curve_tally_equals_the_batch(seed: int) -> None:
    rng = random.Random(seed)  # noqa: S311 - reproducible test data
    equity = Decimal(100_000)
    curve = []
    for index in range(rng.randint(0, 200)):
        equity += Decimal(rng.randint(-5_000, 5_000)) / 100
        curve.append((T0 + index * MINUTE, equity))
    _compare(curve, risk_free=rng.choice([0.0, 1e-7]))


def test_an_irregular_gap_refuses_the_ratios_both_ways() -> None:
    curve = [(T0 + index * MINUTE, Decimal(100 + index)) for index in range(10)]
    curve.append((T0 + 20 * MINUTE, Decimal(90)))
    _compare(curve)
    tally = CurveTally(interval=MINUTE, risk_free_per_period=0.0)
    for point in curve:
        tally.add(*point)
    assert tally.figures(minimum=3).return_observations == 0


def test_non_positive_equity_refuses_returns_both_ways() -> None:
    _compare([(T0 + index * MINUTE, Decimal(value)) for index, value in enumerate([5, 0, 3, 4, 6])])


def test_a_snapshot_retried_in_its_interval_replaces_its_point() -> None:
    """The database upserts the same captured_at; the tally must agree."""
    tally = CurveTally(interval=MINUTE, risk_free_per_period=0.0)
    for index, value in enumerate([100, 110, 90]):
        tally.add(T0 + index * MINUTE, Decimal(value))
    tally.add(T0 + 2 * MINUTE, Decimal(120))
    batch = curve_figures(
        [(T0, Decimal(100)), (T0 + MINUTE, Decimal(110)), (T0 + 2 * MINUTE, Decimal(120))],
        interval=MINUTE,
        risk_free_per_period=0.0,
        minimum=2,
    )
    folded = tally.figures(minimum=2)
    assert folded.max_drawdown_usd == batch.max_drawdown_usd == Decimal(0)
    assert folded.total_return_pct == batch.total_return_pct
    assert folded.return_observations == batch.return_observations == 2
    assert folded.sharpe_ratio is not None and batch.sharpe_ratio is not None
    assert math.isclose(folded.sharpe_ratio, batch.sharpe_ratio, rel_tol=1e-9)


def test_an_empty_curve_has_no_figures() -> None:
    tally = CurveTally(interval=MINUTE, risk_free_per_period=0.0)
    assert tally.figures(minimum=2) == curve_figures(
        [], interval=MINUTE, risk_free_per_period=0.0, minimum=2
    )
