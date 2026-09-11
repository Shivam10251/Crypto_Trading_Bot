"""The cost model: the real fee schedule, a measured exit, and discrete funding."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from trading_bot.core.config import CostsConfig
from trading_bot.db.models.enums import MarketType, Side
from trading_bot.exchange.models import FundingInfo, MarketRef
from trading_bot.strategy.costs import TransactionCostModel, settlements_crossed
from trading_bot.strategy.fees import FeeSchedule, OrderRole
from trading_bot.strategy.models import Leg, Opportunity

NOW = datetime(2026, 9, 11, 16, 30, tzinfo=UTC)
SPOT = MarketRef("binance", "BTCUSDT", MarketType.SPOT)
PERP = MarketRef("binance", "BTCUSDT", MarketType.PERPETUAL)
COSTS = CostsConfig(safety_buffer_bps=2.0)


def model(**overrides: object) -> TransactionCostModel:
    return TransactionCostModel(CostsConfig(**overrides))  # type: ignore[arg-type]


def funding(
    rate: str = "0",
    *,
    interval: int | None = 8,
    next_at: datetime | None = None,
) -> FundingInfo:
    return FundingInfo(
        ref=PERP,
        mark_price=Decimal(100),
        index_price=Decimal(100),
        last_funding_rate=Decimal(rate),
        next_funding_time=next_at or NOW + timedelta(hours=4),
        local_timestamp=NOW,
        funding_interval_hours=interval,
    )


def opportunity(
    *,
    spot_mid: str = "100",
    perp_mid: str = "101",
    spot_exec: str = "100",
    perp_exec: str = "101",
    spot_exit: str | None = None,
    perp_exit: str | None = None,
    quantity: str = "10",
) -> Opportunity:
    """Perp rich: buy spot, sell perp."""
    buy = Leg(
        ref=SPOT,
        side=Side.BUY,
        reference_price=Decimal(spot_mid),
        executable_price=Decimal(spot_exec),
        quantity=Decimal(quantity),
        exit_price=Decimal(spot_exit) if spot_exit else None,
    )
    sell = Leg(
        ref=PERP,
        side=Side.SELL,
        reference_price=Decimal(perp_mid),
        executable_price=Decimal(perp_exec),
        quantity=Decimal(quantity),
        exit_price=Decimal(perp_exit) if perp_exit else None,
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


class TestFeeSchedule:
    def test_vip0_defaults_match_the_published_schedule(self) -> None:
        fees = FeeSchedule.from_config(CostsConfig())
        assert fees.spot.maker_bps == 10 and fees.spot.taker_bps == 10
        assert fees.perpetual.maker_bps == 2 and fees.perpetual.taker_bps == 5

    def test_spot_maker_equals_spot_taker(self) -> None:
        """The fact that decides whether limit orders help: on spot they do not."""
        fees = FeeSchedule.from_config(CostsConfig())
        assert fees.spot.maker_bps == fees.spot.taker_bps

    def test_bnb_discounts_the_legs_by_different_amounts(self) -> None:
        fees = FeeSchedule.from_config(CostsConfig(pay_fees_in_bnb=True))
        assert fees.spot.taker_bps == Decimal("7.5")  # 25% off
        assert fees.perpetual.taker_bps == Decimal("4.5")  # 10% off
        assert fees.perpetual.maker_bps == Decimal("1.8")

    def test_the_best_possible_round_trip_is_18_6_bps(self) -> None:
        """The floor: BNB discount, maker on both legs, entry and exit.

        It matters because the average gross basis measured live is 13 bps -
        below this floor before slippage is charged at all.
        """
        fees = FeeSchedule.from_config(CostsConfig(pay_fees_in_bnb=True))
        floor = (fees.spot.maker_bps + fees.perpetual.maker_bps) * 2
        assert floor == Decimal("18.6")

    def test_a_venue_reported_rate_beats_configuration(self) -> None:
        from trading_bot.exchange.models import MarketSpec

        spec = MarketSpec(
            ref=SPOT,
            base_asset="BTC",
            quote_asset="USDT",
            is_active=True,
            taker_fee_bps=Decimal("3"),
        )
        fees = FeeSchedule.from_config(CostsConfig())
        assert fees.rate_bps(SPOT, OrderRole.TAKER, spec) == Decimal(3)
        # ...but only where the venue actually reports one.
        assert fees.rate_bps(SPOT, OrderRole.MAKER, spec) == Decimal(10)


class TestDiscreteFunding:
    def test_a_hold_that_crosses_no_settlement_pays_nothing(self) -> None:
        """Measured live: a 60-min BTC hold at 16:51 UTC crosses zero.

        The Phase 5 model charged 0.125 of a settlement for exactly this case -
        a cost that would never have been paid.
        """
        info = funding(next_at=NOW + timedelta(hours=4))
        assert settlements_crossed(info, NOW, timedelta(hours=1)) == 0

    def test_a_hold_that_crosses_one_settlement_pays_one(self) -> None:
        info = funding(next_at=NOW + timedelta(minutes=30))
        assert settlements_crossed(info, NOW, timedelta(hours=1)) == 1

    def test_a_long_hold_crosses_several(self) -> None:
        info = funding(interval=8, next_at=NOW + timedelta(hours=1))
        assert settlements_crossed(info, NOW, timedelta(hours=24)) == 3

    def test_a_four_hour_market_settles_twice_as_often(self) -> None:
        eight = funding(interval=8, next_at=NOW + timedelta(hours=1))
        four = funding(interval=4, next_at=NOW + timedelta(hours=1))
        window = timedelta(hours=24)
        assert settlements_crossed(four, NOW, window) == 6
        assert settlements_crossed(eight, NOW, window) == 3

    def test_a_stale_schedule_is_wound_forward(self) -> None:
        """A funding poll from hours ago must not charge settlements already past.

        The tracker polls slowly, so ``next_funding_time`` can be behind us.
        Counting from it directly would charge for every settlement since.
        """
        info = funding(interval=1, next_at=NOW - timedelta(hours=3, minutes=30))
        # Settlements land at :30 past each hour, so the next still ahead is
        # NOW+30m: a one-hour hold crosses exactly that one, not the four past.
        assert settlements_crossed(info, NOW, timedelta(hours=1)) == 1
        assert settlements_crossed(info, NOW, timedelta(hours=2)) == 2

    def test_an_unknown_interval_has_no_answer(self) -> None:
        assert settlements_crossed(funding(interval=None), NOW, timedelta(hours=1)) is None

    def test_funding_is_charged_only_for_settlements_crossed(self) -> None:
        far = model().estimate(opportunity(), funding("0.0001", next_at=NOW + timedelta(hours=4)))
        near = model().estimate(
            opportunity(), funding("0.0001", next_at=NOW + timedelta(minutes=10))
        )
        assert far is not None and near is not None
        assert far.costs.funding_usd == 0  # no settlement inside the hold
        assert near.costs.funding_usd != 0

    def test_short_perpetual_receives_funding_when_the_rate_is_positive(self) -> None:
        edge = model().estimate(
            opportunity(), funding("0.0001", next_at=NOW + timedelta(minutes=10))
        )
        assert edge is not None
        assert edge.costs.funding_usd == -(Decimal(1010) * Decimal("0.0001"))

    def test_an_unpublished_interval_is_refused_rather_than_guessed(self) -> None:
        assert model().estimate(opportunity(), funding("0.0001", interval=None)) is None

    def test_missing_funding_data_is_refused_too(self) -> None:
        assert model().estimate(opportunity(), None) is None


class TestMeasuredExit:
    def test_the_exit_is_walked_not_doubled(self) -> None:
        """Phase 5 charged the entry's slippage twice; the exit is its own walk."""
        # Entry: buy spot 0.05 above mid, sell perp 0.05 below.
        # Exit:  sell spot 0.02 below mid, buy perp 0.02 above - a tighter book.
        edge = model().estimate(
            opportunity(
                spot_exec="100.05", perp_exec="100.95", spot_exit="99.98", perp_exit="101.02"
            ),
            funding("0"),
        )
        assert edge is not None
        entry = Decimal("0.05") * Decimal(10) * 2
        measured_exit = Decimal("0.02") * Decimal(10) * 2
        assert edge.costs.slippage_usd == entry + measured_exit
        # Strictly less than the Phase 5 assumption of charging entry twice.
        assert edge.costs.slippage_usd < entry * 2

    def test_an_unfillable_exit_falls_back_to_charging_the_entry_again(self) -> None:
        """Dropping the cost entirely would be worse than the old assumption."""
        edge = model().estimate(opportunity(spot_exec="100.05", perp_exec="100.95"), funding("0"))
        assert edge is not None
        entry = Decimal("0.05") * Decimal(10) * 2
        assert edge.costs.slippage_usd == entry * 2

    def test_a_favourable_exit_is_never_counted_as_negative_slippage(self) -> None:
        edge = model().estimate(opportunity(spot_exit="100.50", perp_exit="100.50"), funding("0"))
        assert edge is not None
        assert edge.costs.slippage_usd == 0


class TestRoles:
    def test_taker_on_both_sides_is_the_default(self) -> None:
        assert model().round_trip_fee_bps(opportunity()) == Decimal(30)

    def test_maker_saves_nothing_on_spot_but_helps_the_perpetual(self) -> None:
        maker = model(entry_role="maker", exit_role="maker")
        # spot 10+10 unchanged; perp 5+5 becomes 2+2.
        assert maker.round_trip_fee_bps(opportunity()) == Decimal(24)

    def test_the_bnb_discount_lowers_the_floor_further(self) -> None:
        cheapest = model(entry_role="maker", exit_role="maker", pay_fees_in_bnb=True)
        assert cheapest.round_trip_fee_bps(opportunity()) == Decimal("18.6")

    def test_entry_and_exit_roles_can_differ(self) -> None:
        mixed = model(entry_role="taker", exit_role="maker")
        assert mixed.round_trip_fee_bps(opportunity()) == Decimal(27)


class TestNetEdge:
    def test_net_edge_is_gross_minus_every_cost(self) -> None:
        edge = model().estimate(opportunity(), funding("0"))
        assert edge is not None
        assert edge.net_edge_usd == edge.gross_edge_usd - edge.costs.total_usd

    def test_a_typical_live_basis_does_not_survive_even_the_cheapest_fees(self) -> None:
        """13 bps of basis against an 18.6 bps floor - the Phase 6 conclusion."""
        cheapest = model(entry_role="maker", exit_role="maker", pay_fees_in_bnb=True)
        thin = opportunity(spot_mid="100", perp_mid="100.13")  # 13 bps
        edge = cheapest.estimate(thin, funding("0"))
        assert edge is not None
        assert edge.gross_edge_bps == Decimal(13)
        assert not edge.is_profitable

    def test_a_wide_enough_basis_still_clears(self) -> None:
        edge = model().estimate(opportunity(), funding("0"))  # 100 bps
        assert edge is not None
        assert edge.is_profitable

    def test_describe_states_every_assumption(self) -> None:
        text = model().describe()
        for fragment in ("maker/taker", "taker in", "settlements crossed", "buffer"):
            assert fragment in text


def test_an_invalid_role_is_rejected_by_configuration() -> None:
    with pytest.raises(ValueError):
        CostsConfig(entry_role="both")  # type: ignore[arg-type]
