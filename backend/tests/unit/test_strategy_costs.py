"""The cost model: what stands between a gross spread and money kept."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from trading_bot.core.config import CostsConfig
from trading_bot.db.models.enums import MarketType, Side
from trading_bot.exchange.models import FundingInfo, MarketRef
from trading_bot.strategy.costs import ConfiguredCostModel
from trading_bot.strategy.models import Leg, Opportunity

NOW = datetime(2026, 9, 11, 16, 30, tzinfo=UTC)
SPOT = MarketRef("binance", "BTCUSDT", MarketType.SPOT)
PERP = MarketRef("binance", "BTCUSDT", MarketType.PERPETUAL)
COSTS = CostsConfig(spot_taker_fee_bps=10.0, perp_taker_fee_bps=5.0, safety_buffer_bps=2.0)


def model(hours: float = 1.0) -> ConfiguredCostModel:
    return ConfiguredCostModel(COSTS, funding_horizon=timedelta(hours=hours))


def funding(rate: str, *, interval: int | None = 8) -> FundingInfo:
    return FundingInfo(
        ref=PERP,
        mark_price=Decimal(100),
        index_price=Decimal(100),
        last_funding_rate=Decimal(rate),
        next_funding_time=NOW + timedelta(hours=4),
        local_timestamp=NOW,
        funding_interval_hours=interval,
    )


def opportunity(
    *,
    spot_mid: str = "100",
    perp_mid: str = "101",
    spot_exec: str = "100",
    perp_exec: str = "101",
    quantity: str = "10",
) -> Opportunity:
    """Perp rich: buy spot, sell perp."""
    buy = Leg(
        ref=SPOT,
        side=Side.BUY,
        reference_price=Decimal(spot_mid),
        executable_price=Decimal(spot_exec),
        quantity=Decimal(quantity),
    )
    sell = Leg(
        ref=PERP,
        side=Side.SELL,
        reference_price=Decimal(perp_mid),
        executable_price=Decimal(perp_exec),
        quantity=Decimal(quantity),
    )
    gross_per_unit = Decimal(perp_mid) - Decimal(spot_mid)
    return Opportunity(
        strategy="spot_perp_basis",
        detected_at=NOW,
        buy=buy,
        sell=sell,
        reference_price=Decimal(spot_mid),
        quantity=Decimal(quantity),
        notional_usd=Decimal(spot_mid) * Decimal(quantity),
        gross_edge_bps=gross_per_unit / Decimal(spot_mid) * Decimal(10_000),
        gross_edge_usd=gross_per_unit * Decimal(quantity),
    )


def test_fees_are_charged_on_both_legs_twice() -> None:
    """Entry and exit, both legs: (10 + 5) x 2 = 30 bps of notional."""
    edge = model().estimate(opportunity(), funding("0"))
    assert edge is not None
    # 1000 notional on spot, 1010 on perp; each pays its own fee, twice.
    expected = Decimal(1000) * Decimal("0.0010") * 2 + Decimal(1010) * Decimal("0.0005") * 2
    assert edge.costs.fees_usd == expected


def test_slippage_is_measured_and_charged_for_the_round_trip() -> None:
    """Crossing to executable prices costs on the way in and again on the way out."""
    # Buying spot 0.05 above the mid and selling perp 0.05 below it.
    edge = model().estimate(opportunity(spot_exec="100.05", perp_exec="100.95"), funding("0"))
    assert edge is not None
    entry = Decimal("0.05") * Decimal(10) * 2  # both legs
    assert edge.costs.slippage_usd == entry * 2  # entry + exit


def test_a_favourable_fill_is_never_counted_as_negative_slippage() -> None:
    """Executing better than the mid is not edge the strategy may claim."""
    edge = model().estimate(opportunity(spot_exec="99.90", perp_exec="101.10"), funding("0"))
    assert edge is not None
    assert edge.costs.slippage_usd == 0


def test_short_perpetual_receives_funding_when_the_rate_is_positive() -> None:
    """The strategy is short the rich perp, so positive funding is a credit."""
    edge = model(hours=8).estimate(opportunity(), funding("0.0001"))
    assert edge is not None
    # One full 8h interval on 1010 of perp notional, received rather than paid.
    assert edge.costs.funding_usd == -(Decimal(1010) * Decimal("0.0001"))
    assert edge.costs.funding_usd < 0


def test_long_perpetual_pays_funding_when_the_rate_is_positive() -> None:
    """Perp at a discount: we buy it, and then positive funding is a cost."""
    cheap = opportunity(spot_mid="101", perp_mid="100", spot_exec="101", perp_exec="100")
    # Rebuild with the perpetual as the bought leg.
    buy = Leg(
        ref=PERP,
        side=Side.BUY,
        reference_price=Decimal(100),
        executable_price=Decimal(100),
        quantity=Decimal(10),
    )
    sell = Leg(
        ref=SPOT,
        side=Side.SELL,
        reference_price=Decimal(101),
        executable_price=Decimal(101),
        quantity=Decimal(10),
    )
    inverted = Opportunity(
        strategy=cheap.strategy,
        detected_at=NOW,
        buy=buy,
        sell=sell,
        reference_price=Decimal(101),
        quantity=Decimal(10),
        notional_usd=Decimal(1010),
        gross_edge_bps=cheap.gross_edge_bps,
        gross_edge_usd=cheap.gross_edge_usd,
    )
    edge = model(hours=8).estimate(inverted, funding("0.0001"))
    assert edge is not None
    assert edge.costs.funding_usd == Decimal(1000) * Decimal("0.0001")
    assert edge.costs.funding_usd > 0


def test_funding_scales_with_the_venues_interval_not_a_assumed_eight_hours() -> None:
    """A 4h market pays the same rate twice as often - measured, not assumed.

    Binance settles 467 USD-M perpetuals every four hours and 313 every eight.
    Treating them alike would halve the funding charged on the majority.
    """
    over_8h = model(hours=8)
    eight = over_8h.estimate(opportunity(), funding("0.0001", interval=8))
    four = over_8h.estimate(opportunity(), funding("0.0001", interval=4))
    assert eight is not None and four is not None
    assert four.costs.funding_usd == eight.costs.funding_usd * 2


def test_an_unpublished_funding_interval_is_refused_rather_than_guessed() -> None:
    """No interval means no honest cost, so there is no net edge to report."""
    assert model().estimate(opportunity(), funding("0.0001", interval=None)) is None


def test_missing_funding_data_is_refused_too() -> None:
    assert model().estimate(opportunity(), None) is None


def test_net_edge_is_gross_minus_every_cost() -> None:
    edge = model().estimate(opportunity(), funding("0"))
    assert edge is not None
    assert edge.net_edge_usd == edge.gross_edge_usd - edge.costs.total_usd
    # A 100 bps gross basis cannot survive 30 bps of fees plus the buffer...
    assert edge.gross_edge_bps == Decimal(100)
    assert edge.is_profitable  # ...at this width, it does


def test_a_typical_live_basis_does_not_survive_costs() -> None:
    """The measured case: a few bps of basis against 30 bps of round-trip fees."""
    thin = opportunity(spot_mid="100", perp_mid="100.05")  # 5 bps
    edge = model().estimate(thin, funding("0"))
    assert edge is not None
    assert edge.gross_edge_bps == Decimal(5)
    assert not edge.is_profitable
    assert edge.net_edge_bps < Decimal(-25)


def test_round_trip_fee_floor_is_stated_in_bps() -> None:
    assert model().round_trip_fee_bps(opportunity()) == Decimal(30)


def test_a_negative_horizon_is_rejected() -> None:
    with pytest.raises(ValueError, match="funding_horizon"):
        ConfiguredCostModel(COSTS, funding_horizon=timedelta(hours=-1))


def test_describe_states_the_assumptions() -> None:
    text = model().describe()
    assert "taker" in text and "funding over" in text and "buffer" in text
