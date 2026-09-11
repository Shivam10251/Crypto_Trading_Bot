"""The strategy panel: ranked by what survives costs, and explains itself."""

from __future__ import annotations

from datetime import UTC, datetime

from tests.unit.test_spot_perp_basis import PERP, SPOT, funding_info, view
from trading_bot.core.config import CostsConfig, SpotPerpBasisConfig
from trading_bot.marketdata.models import BookStatus
from trading_bot.monitoring.strategy_view import render_evaluation
from trading_bot.strategy.base import StrategyContext
from trading_bot.strategy.basis import SpotPerpBasisStrategy
from trading_bot.strategy.costs import TransactionCostModel
from trading_bot.strategy.runner import StrategyRunner

NOW = datetime(2026, 9, 11, 16, 30, tzinfo=UTC)
FREE = CostsConfig(spot_taker_fee_bps=0.0, perp_taker_fee_bps=0.0, safety_buffer_bps=0.0)


def evaluate(costs: CostsConfig, snapshots: list[object], interval: int | None = 8):  # type: ignore[no-untyped-def]
    context = StrategyContext(cost_model=TransactionCostModel(costs))
    runner = StrategyRunner(
        [SpotPerpBasisStrategy(SpotPerpBasisConfig())], context, clock=lambda: NOW
    )
    runner.set_funding({PERP: funding_info(interval=interval)})
    return runner.evaluate(snapshots)[0]


TRADEABLE = [view(SPOT, "99.99", "100.01").snapshot, view(PERP, "100.99", "101.01").snapshot]


def test_a_tradeable_row_says_so() -> None:
    frame = render_evaluation(evaluate(FREE, TRADEABLE), cost_summary="test costs")
    assert "TRADEABLE" in frame
    assert "BTCUSDT" in frame
    assert "tradeable 1" in frame


def test_costs_appear_as_their_own_columns() -> None:
    """A row has to explain its own verdict, not just state it."""
    frame = render_evaluation(evaluate(CostsConfig(), TRADEABLE), cost_summary="test costs")
    for column in ("FEES", "SLIP", "FUND", "BUF", "NET bps", "BASIS bps"):
        assert column in frame


def test_a_rejected_row_names_the_reason() -> None:
    thin = [
        view(SPOT, "99.999", "100.001").snapshot,
        view(PERP, "100.049", "100.051").snapshot,
    ]
    frame = render_evaluation(evaluate(CostsConfig(), thin), cost_summary="test costs")
    assert "below min edge" in frame
    assert "tradeable 0" in frame


def test_the_cost_assumptions_are_printed() -> None:
    """The operator should not have to read the source to know what was charged."""
    model = TransactionCostModel(CostsConfig())
    frame = render_evaluation(evaluate(CostsConfig(), TRADEABLE), cost_summary=model.describe())
    assert "maker/taker" in frame and "settlements crossed" in frame


def test_an_empty_result_explains_why_rather_than_showing_nothing() -> None:
    unusable = [
        view(SPOT, "99.99", "100.01", book_status=BookStatus.SYNCING).snapshot,
        view(PERP, "100.99", "101.01").snapshot,
    ]
    frame = render_evaluation(evaluate(FREE, unusable), cost_summary="test costs")
    assert "0/1 pairs usable" in frame
    assert "book not synced" in frame
    assert "no pair had both legs live" in frame


def test_markets_with_unknown_funding_intervals_are_named() -> None:
    frame = render_evaluation(
        evaluate(FREE, TRADEABLE),
        cost_summary="test costs",
        funding_unknown=["MYROUSDT"],
    )
    assert "not priced rather than assumed" in frame
    assert "MYROUSDT" in frame


def test_rows_are_ranked_by_net_edge() -> None:
    from trading_bot.exchange.models import MarketRef

    eth_spot = MarketRef(SPOT.venue, "ETHUSDT", SPOT.market_type)
    eth_perp = MarketRef(PERP.venue, "ETHUSDT", PERP.market_type)
    context = StrategyContext(cost_model=TransactionCostModel(FREE))
    runner = StrategyRunner(
        [SpotPerpBasisStrategy(SpotPerpBasisConfig())], context, clock=lambda: NOW
    )
    runner.set_funding({PERP: funding_info(interval=8), eth_perp: funding_info(interval=8)})
    evaluation = runner.evaluate(
        [
            view(SPOT, "99.99", "100.01").snapshot,
            view(PERP, "100.09", "100.11").snapshot,  # 10 bps
            view(eth_spot, "99.99", "100.01").snapshot,
            view(eth_perp, "100.99", "101.01").snapshot,  # 100 bps
        ]
    )[0]
    frame = render_evaluation(evaluation, cost_summary="test costs")
    rows = [line for line in frame.splitlines() if line.startswith(("BTCUSDT", "ETHUSDT"))]
    assert rows[0].startswith("ETHUSDT")  # the wider net edge leads


def test_max_rows_truncates_with_a_count() -> None:
    frame = render_evaluation(evaluate(FREE, TRADEABLE), cost_summary="c", max_rows=0)
    assert "... 1 more pairs" in frame
