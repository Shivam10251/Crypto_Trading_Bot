"""The runner: pipeline walk, rejection accounting and strategy construction."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from tests.unit.test_spot_perp_basis import PERP, SPOT, funding_info, view
from trading_bot.core.config import CostsConfig, SpotPerpBasisConfig, StrategyConfig
from trading_bot.exchange.models import MarketRef
from trading_bot.strategy.base import StrategyContext
from trading_bot.strategy.basis import SpotPerpBasisStrategy
from trading_bot.strategy.costs import TransactionCostModel
from trading_bot.strategy.models import RejectionReason
from trading_bot.strategy.registry import UnknownStrategyError, build_strategies
from trading_bot.strategy.runner import StrategyRunner

NOW = datetime(2026, 9, 11, 16, 30, tzinfo=UTC)
FREE = CostsConfig(spot_taker_fee_bps=0.0, perp_taker_fee_bps=0.0, safety_buffer_bps=0.0)


def runner(costs: CostsConfig = FREE, *, funding_interval: int | None = 8) -> StrategyRunner:
    context = StrategyContext(cost_model=TransactionCostModel(costs))
    instance = StrategyRunner(
        [SpotPerpBasisStrategy(SpotPerpBasisConfig())], context, clock=lambda: NOW
    )
    instance.set_funding({PERP: funding_info(interval=funding_interval)})
    return instance


def snapshots(spot: tuple[str, str], perp: tuple[str, str]) -> list[object]:
    return [view(SPOT, *spot).snapshot, view(PERP, *perp).snapshot]


def test_a_tradeable_basis_comes_through_as_actionable() -> None:
    evaluation, *_ = runner().evaluate(snapshots(("99.99", "100.01"), ("100.99", "101.01")))
    assert len(evaluation.opportunities) == 1
    assert len(evaluation.actionable) == 1
    item = evaluation.actionable[0]
    assert item.signal is not None
    assert item.validation is not None and item.validation.is_valid
    assert item.rejection is None


def test_a_thin_basis_is_recorded_as_rejected_not_discarded() -> None:
    """The rejected ones are the research dataset; they must survive the walk."""
    evaluation, *_ = runner(CostsConfig()).evaluate(
        snapshots(("99.999", "100.001"), ("100.049", "100.051"))
    )
    assert len(evaluation.opportunities) == 1
    assert evaluation.actionable == ()
    item = evaluation.opportunities[0]
    assert item.rejection is RejectionReason.BELOW_MIN_EDGE
    assert item.edge is not None  # priced, then rejected - not simply dropped
    assert evaluation.rejections[RejectionReason.BELOW_MIN_EDGE] == 1


def test_an_unpriceable_opportunity_is_recorded_with_its_reason() -> None:
    evaluation, *_ = runner(funding_interval=None).evaluate(
        snapshots(("99.99", "100.01"), ("100.99", "101.01"))
    )
    item = evaluation.opportunities[0]
    assert item.rejection is RejectionReason.FUNDING_UNKNOWN
    assert item.edge is None
    assert item.detail is not None and "funding interval" in item.detail


def test_the_best_opportunity_is_the_best_net_not_the_widest_gross() -> None:
    """A wide spread that costs more to cross is not the better trade."""
    instance = runner(CostsConfig())
    eth_spot = MarketRef(SPOT.venue, "ETHUSDT", SPOT.market_type)
    eth_perp = MarketRef(PERP.venue, "ETHUSDT", PERP.market_type)
    instance.set_funding({PERP: funding_info(interval=8), eth_perp: funding_info(interval=8)})
    evaluation, *_ = instance.evaluate(
        [
            # Wide gross basis, but both books are wide too.
            view(SPOT, "99.00", "101.00").snapshot,
            view(PERP, "101.50", "103.50").snapshot,
            # Narrower basis on tight books.
            view(eth_spot, "99.99", "100.01").snapshot,
            view(eth_perp, "100.59", "100.61").snapshot,
        ]
    )
    best = evaluation.best()
    assert best is not None
    assert best.opportunity.buy.ref.symbol == "ETHUSDT"


def test_detection_stats_explain_an_empty_result() -> None:
    """Zero opportunities and a broken feed must not look the same."""
    from trading_bot.marketdata.models import BookStatus

    evaluation, *_ = runner().evaluate(
        [
            view(SPOT, "99.99", "100.01", book_status=BookStatus.SYNCING).snapshot,
            view(PERP, "100.99", "101.01").snapshot,
        ]
    )
    assert evaluation.opportunities == ()
    assert evaluation.stats is not None
    assert evaluation.stats.pairs_seen == 1
    assert evaluation.stats.pairs_usable == 0
    assert evaluation.stats.unusable[RejectionReason.BOOK_NOT_SYNCED] == 1


def test_evaluations_are_kept_for_the_display_to_read() -> None:
    instance = runner()
    assert instance.evaluations() == []
    instance.evaluate(snapshots(("99.99", "100.01"), ("100.99", "101.01")))
    assert len(instance.evaluations()) == 1


# --- registry -------------------------------------------------------------


def test_the_configured_strategy_is_built() -> None:
    (strategy,) = build_strategies(StrategyConfig())
    assert isinstance(strategy, SpotPerpBasisStrategy)
    assert strategy.name == "spot_perp_basis"


def test_an_unknown_strategy_name_fails_at_startup() -> None:
    """Quietly running fewer strategies than configured would be worse."""
    with pytest.raises(UnknownStrategyError, match="momentum"):
        build_strategies(StrategyConfig(enabled=["momentum"]))


def test_no_enabled_strategies_builds_nothing() -> None:
    assert build_strategies(StrategyConfig(enabled=[])) == []


def test_costs_are_reported_in_bps_of_notional() -> None:
    evaluation, *_ = runner(CostsConfig()).evaluate(
        snapshots(("99.99", "100.01"), ("100.99", "101.01"))
    )
    item = evaluation.opportunities[0]
    assert item.edge is not None
    notional = item.opportunity.notional_usd
    fees_bps = item.edge.costs.fees_usd / notional * Decimal(10_000)
    # 10 + 5 bps per leg pair, entry and exit, on roughly equal notionals.
    assert Decimal(29) < fees_bps < Decimal(31)
