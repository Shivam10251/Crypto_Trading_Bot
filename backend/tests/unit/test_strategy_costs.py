"""The cost model: the real fee schedule, a measured exit, and discrete funding."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from trading_bot.core.config import CostsConfig
from trading_bot.db.models.enums import MarketType, Side
from trading_bot.exchange.models import FundingInfo, MarketRef, MarketSpec
from trading_bot.strategy.costs import (
    COST_MODEL_VERSION,
    TransactionCostModel,
    settlements_crossed,
)
from trading_bot.strategy.fees import FeeSchedule, OrderRole
from trading_bot.strategy.models import Edge, Leg, Opportunity, PricingRefusal, RejectionReason


def priced(result: object) -> Edge:
    """The edge, insisting the model did not refuse to price it."""
    assert isinstance(result, Edge), result
    return result


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
    spot_unwind: str | None = None,
    perp_unwind: str | None = None,
    unwind_fillable: bool = True,
    quantity: str = "10",
) -> Opportunity:
    """Perp rich: buy spot, sell perp.

    Both unwinds are priced by default - the strategy only produces an
    opportunity at a quantity both books can close - and default to the mid,
    so unwind slippage is zero unless a test asks for some.
    """
    buy = Leg(
        ref=SPOT,
        side=Side.BUY,
        reference_price=Decimal(spot_mid),
        executable_price=Decimal(spot_exec),
        quantity=Decimal(quantity),
        unwind_price=Decimal(spot_unwind or spot_mid) if unwind_fillable else None,
    )
    sell = Leg(
        ref=PERP,
        side=Side.SELL,
        reference_price=Decimal(perp_mid),
        executable_price=Decimal(perp_exec),
        quantity=Decimal(quantity),
        unwind_price=Decimal(perp_unwind or perp_mid) if unwind_fillable else None,
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


class TestVenueFeeOverrides:
    """``FeeSchedule`` could always prefer a venue rate; nothing passed it one.

    The production path built ``TransactionCostModel(settings.costs)`` with no
    specs, so ``rate_bps`` was called without the ``MarketSpec`` that carries
    them and configuration won every time - the override was unreachable
    outside its own unit test.
    """

    def spec(self, ref: MarketRef, **rates: str) -> MarketSpec:
        return MarketSpec(
            ref=ref,
            base_asset="BTC",
            quote_asset="USDT",
            is_active=True,
            **{key: Decimal(value) for key, value in rates.items()},  # type: ignore[arg-type]
        )

    def test_without_specs_the_configured_schedule_decides(self) -> None:
        assert model().round_trip_fee_bps(opportunity()) == Decimal(30)

    def test_a_venue_taker_rate_reaches_the_real_calculation(self) -> None:
        costed = TransactionCostModel(
            CostsConfig(), specs={SPOT: self.spec(SPOT, taker_fee_bps="3")}
        )
        # spot 3+3 instead of 10+10; the perpetual still 5+5 from config.
        assert costed.round_trip_fee_bps(opportunity()) == Decimal(16)
        edge = priced(costed.estimate(opportunity(), funding("0")))
        assert (
            edge.costs.fees_usd
            < priced(model().estimate(opportunity(), funding("0"))).costs.fees_usd
        )

    def test_a_venue_maker_rate_reaches_it_too(self) -> None:
        costed = TransactionCostModel(
            CostsConfig(entry_role="maker", exit_role="maker"),
            specs={PERP: self.spec(PERP, maker_fee_bps="1")},
        )
        # spot 10+10 from config, perpetual 1+1 from the venue.
        assert costed.round_trip_fee_bps(opportunity()) == Decimal(22)

    def test_one_leg_overridden_leaves_the_other_on_configuration(self) -> None:
        costed = TransactionCostModel(
            CostsConfig(), specs={SPOT: self.spec(SPOT, taker_fee_bps="3")}
        )
        edge = priced(costed.estimate(opportunity(), funding("0")))
        assert edge.pricing is not None
        by_type = {fee.market_type: fee for fee in edge.pricing.fees}
        assert by_type["SPOT"].entry_rate_bps == Decimal(3)
        assert by_type["SPOT"].venue_reported is True
        assert by_type["PERPETUAL"].entry_rate_bps == Decimal(5)
        assert by_type["PERPETUAL"].venue_reported is False

    def test_the_bnb_discount_is_not_applied_to_a_venue_rate(self) -> None:
        """A rate the venue reports is what the account pays, discount included.

        Taking 25% off it again would double-count: the configured 10 bps
        becomes 7.5, but a reported 3 bps stays 3.
        """
        costed = TransactionCostModel(
            CostsConfig(pay_fees_in_bnb=True),
            specs={SPOT: self.spec(SPOT, taker_fee_bps="3")},
        )
        assert costed.fees.spot.taker_bps == Decimal("7.5")  # configured, discounted
        edge = priced(costed.estimate(opportunity(), funding("0")))
        assert edge.pricing is not None
        spot_fee = next(fee for fee in edge.pricing.fees if fee.market_type == "SPOT")
        assert spot_fee.entry_rate_bps == Decimal(3)  # reported, not 2.25
        # ...and the perpetual, which the venue does not report, is discounted.
        perp_fee = next(fee for fee in edge.pricing.fees if fee.market_type == "PERPETUAL")
        assert perp_fee.entry_rate_bps == Decimal("4.5")


class TestAssumptions:
    def test_the_convergence_assumption_is_recorded_not_implied(self) -> None:
        """The gross edge assumes the basis converges. That is an assumption."""
        edge = priced(model().estimate(opportunity(), funding("0")))
        assert edge.pricing is not None
        assumptions = edge.pricing.assumptions
        assert Decimal(assumptions["assumed_terminal_basis_bps"]) == 0
        assert "not realised profit" in assumptions["gross_edge_is"]
        assert assumptions["settlement_window"] == "(open, open + horizon]"

    def test_a_non_zero_terminal_basis_is_charged_as_a_cost(self) -> None:
        """Assuming less than full convergence makes the edge smaller, never bigger."""
        full = priced(model().estimate(opportunity(), funding("0")))
        partial = priced(
            model(assumed_terminal_basis_bps=20.0).estimate(opportunity(), funding("0"))
        )
        assert partial.net_edge_usd < full.net_edge_usd
        assert partial.costs.other_usd == Decimal(1000) * Decimal(20) / Decimal(10_000)

    def test_the_cost_model_stamps_its_version(self) -> None:
        edge = priced(model().estimate(opportunity(), funding("0")))
        assert edge.pricing is not None
        assert edge.pricing.cost_model_version == COST_MODEL_VERSION


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

    def test_opening_exactly_on_a_settlement_does_not_pay_it(self) -> None:
        """The window is (open, open + horizon]: we were not holding before it."""
        info = funding(interval=8, next_at=NOW)
        assert settlements_crossed(info, NOW, timedelta(hours=1)) == 0

    def test_closing_exactly_on_a_settlement_does_pay_it(self) -> None:
        """...and we were holding right up to this one."""
        info = funding(interval=8, next_at=NOW + timedelta(hours=1))
        assert settlements_crossed(info, NOW, timedelta(hours=1)) == 1

    def test_equivalent_schedules_give_the_same_count(self) -> None:
        """The same schedule described two ways must cost the same.

        ``nextFundingTime`` comes from a poll that may be stale, so the same
        8-hourly grid reaches us as T, as T - 8h, or as T - 24h. Counting from
        an inclusive start charged the first a settlement the others were not
        charged - the position's cost depended on the age of the poll.
        """
        window = timedelta(hours=12)
        counts = {
            settlements_crossed(funding(interval=8, next_at=NOW + offset), NOW, window)
            for offset in (
                timedelta(0),
                timedelta(hours=-8),
                timedelta(hours=-24),
                timedelta(hours=-800),
            )
        }
        assert counts == {1}

    def test_funding_is_charged_only_for_settlements_crossed(self) -> None:
        far = priced(
            model().estimate(opportunity(), funding("0.0001", next_at=NOW + timedelta(hours=4)))
        )
        near = priced(
            model().estimate(opportunity(), funding("0.0001", next_at=NOW + timedelta(minutes=10)))
        )
        assert far.costs.funding_usd == 0  # no settlement inside the hold
        assert near.costs.funding_usd != 0

    def test_funding_is_charged_on_the_mark_notional_not_the_entry(self) -> None:
        """The venue settles mark x size x rate; the spread we crossed is ours.

        Mark is 100 and the perpetual filled at 101, so charging the entry
        notional overstated funding by 1% of it - in the wrong direction for
        a short, which *receives* it.
        """
        edge = priced(
            model().estimate(
                opportunity(perp_exec="101"),
                funding("0.0001", next_at=NOW + timedelta(minutes=10)),
            )
        )
        mark_notional = Decimal(100) * Decimal(10)
        assert edge.costs.funding_usd == -(mark_notional * Decimal("0.0001"))
        assert edge.costs.funding_usd != -(Decimal(1010) * Decimal("0.0001"))

    def test_short_perpetual_receives_funding_when_the_rate_is_positive(self) -> None:
        short = priced(
            model().estimate(opportunity(), funding("0.0001", next_at=NOW + timedelta(minutes=10)))
        )
        assert short.costs.funding_usd < 0

    def test_long_perpetual_pays_funding_when_the_rate_is_positive(self) -> None:
        """Perp at a discount: the perpetual leg is bought, so it pays."""
        cheap = opportunity(spot_mid="101", perp_mid="100")
        long_perp = Leg(
            ref=PERP,
            side=Side.BUY,
            reference_price=Decimal(100),
            executable_price=Decimal(100),
            quantity=Decimal(10),
            unwind_price=Decimal(100),
        )
        spot_sell = Leg(
            ref=SPOT,
            side=Side.SELL,
            reference_price=Decimal(101),
            executable_price=Decimal(101),
            quantity=Decimal(10),
            unwind_price=Decimal(101),
        )
        flipped = replace(cheap, buy=long_perp, sell=spot_sell)
        edge = priced(
            model(spot_borrow_rate_bps_per_day=0).estimate(
                flipped, funding("0.0001", next_at=NOW + timedelta(minutes=10))
            )
        )
        assert edge.costs.funding_usd > 0

    def test_zero_settlements_produce_zero_funding(self) -> None:
        edge = priced(
            model().estimate(opportunity(), funding("0.05", next_at=NOW + timedelta(hours=4)))
        )
        assert edge.costs.funding_usd == 0

    def test_more_than_one_settlement_is_marked_as_an_assumption(self) -> None:
        """The venue announces one rate. Reusing it is a guess, labelled as one."""
        edge = priced(
            model(funding_horizon_minutes=24 * 60).estimate(
                opportunity(), funding("0.0001", next_at=NOW + timedelta(hours=1))
            )
        )
        assert edge.pricing is not None and edge.pricing.funding is not None
        assert edge.pricing.funding.settlements == 3
        assert edge.pricing.funding.rate_assumed_constant is True

    def test_an_unpublished_interval_is_refused_rather_than_guessed(self) -> None:
        refusal = model().estimate(opportunity(), funding("0.0001", interval=None))
        assert isinstance(refusal, PricingRefusal)
        assert refusal.reason is RejectionReason.FUNDING_UNKNOWN

    def test_missing_funding_data_is_refused_too(self) -> None:
        refusal = model().estimate(opportunity(), None)
        assert isinstance(refusal, PricingRefusal)
        assert refusal.reason is RejectionReason.FUNDING_UNKNOWN


class TestMeasuredExit:
    def test_the_exit_is_walked_not_doubled(self) -> None:
        """Phase 5 charged the entry's slippage twice; the exit is its own walk."""
        # Entry: buy spot 0.05 above mid, sell perp 0.05 below.
        # Exit:  sell spot 0.02 below mid, buy perp 0.02 above - a tighter book.
        edge = priced(
            model().estimate(
                opportunity(
                    spot_exec="100.05",
                    perp_exec="100.95",
                    spot_unwind="99.98",
                    perp_unwind="101.02",
                ),
                funding("0"),
            )
        )
        entry = Decimal("0.05") * Decimal(10) * 2
        measured_exit = Decimal("0.02") * Decimal(10) * 2
        assert edge.costs.slippage_usd == entry + measured_exit
        # Strictly less than the Phase 5 assumption of charging entry twice.
        assert edge.costs.slippage_usd < entry * 2

    def test_an_unfillable_unwind_is_refused_not_charged_the_entry_twice(self) -> None:
        """Charging the entry again is a guess, not a conservative estimate.

        Missing exit liquidity is precisely the case where the exit costs
        *more* than the entry did, so substituting the entry's slippage
        understates it while looking careful. There is no exit price, so
        there is no priced round trip.
        """
        refusal = model().estimate(
            opportunity(spot_exec="100.05", perp_exec="100.95", unwind_fillable=False),
            funding("0"),
        )
        assert isinstance(refusal, PricingRefusal)
        assert refusal.reason is RejectionReason.UNWIND_NOT_FILLABLE

    def test_a_favourable_exit_is_never_counted_as_negative_slippage(self) -> None:
        edge = priced(
            model().estimate(opportunity(spot_unwind="100.50", perp_unwind="100.50"), funding("0"))
        )
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
        edge = priced(model().estimate(opportunity(), funding("0")))
        assert edge.net_edge_usd == edge.gross_edge_usd - edge.costs.total_usd

    def test_a_typical_live_basis_does_not_survive_even_the_cheapest_fees(self) -> None:
        """13 bps of basis against an 18.6 bps floor - the Phase 6 conclusion."""
        cheapest = model(entry_role="maker", exit_role="maker", pay_fees_in_bnb=True)
        thin = opportunity(spot_mid="100", perp_mid="100.13")  # 13 bps
        edge = priced(cheapest.estimate(thin, funding("0")))
        assert edge.gross_edge_bps == Decimal(13)
        assert not edge.is_profitable

    def test_a_wide_enough_basis_still_clears(self) -> None:
        edge = priced(model().estimate(opportunity(), funding("0")))  # 100 bps
        assert edge.is_profitable

    def test_describe_states_every_assumption(self) -> None:
        text = model().describe()
        for fragment in ("maker/taker", "taker in", "settlements crossed", "buffer"):
            assert fragment in text


def test_an_invalid_role_is_rejected_by_configuration() -> None:
    with pytest.raises(ValueError):
        CostsConfig(entry_role="both")  # type: ignore[arg-type]
