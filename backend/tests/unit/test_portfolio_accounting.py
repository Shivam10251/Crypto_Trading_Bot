"""P&L arithmetic, against hand-computed numbers.

Every assertion here is a number worked out by hand in the docstring or the
comment above it, not one read back out of the implementation. That is the
only way this file can catch the errors it exists for - a sign flipped on the
short leg, a fee charged twice, slippage subtracted from prices that already
contain it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from trading_bot.db.models.enums import Side
from trading_bot.portfolio.accounting import (
    FUNDING,
    SPOT_BORROW,
    FillLot,
    PairedTrade,
    TradeOutcome,
    max_drawdown,
    position_pnl,
    sample_returns,
    sharpe_ratio,
    sortino_ratio,
    summarise_trades,
    total_return_pct,
    weigh,
)

NOW = datetime(2026, 9, 12, 12, 0, tzinfo=UTC)


def lot(
    price: str, quantity: str, fee: str = "0", *, minutes: int = 0, expected: str | None = None
):  # type: ignore[no-untyped-def]
    return FillLot(
        price=Decimal(price),
        quantity=Decimal(quantity),
        fee_usd=Decimal(fee),
        filled_at=NOW + timedelta(minutes=minutes),
        expected_price=Decimal(expected) if expected is not None else None,
    )


class TestWeighting:
    def test_weighted_price_is_notional_over_quantity(self) -> None:
        """(100*1 + 110*3) / 4 = 107.5, not the 105 a simple mean would give."""
        weighted = weigh(Side.BUY, [lot("100", "1"), lot("110", "3")])

        assert weighted.quantity == Decimal(4)
        assert weighted.price == Decimal("107.5")

    def test_an_empty_set_has_no_price(self) -> None:
        weighted = weigh(Side.BUY, [])

        assert weighted.price is None
        assert weighted.quantity == 0

    def test_slippage_is_adverse_positive_on_both_sides(self) -> None:
        """A buy above expectation and a sell below it both cost us."""
        bought = weigh(Side.BUY, [lot("101", "2", expected="100")])
        sold = weigh(Side.SELL, [lot("99", "2", expected="100")])

        assert bought.slippage_usd == Decimal(2)
        assert sold.slippage_usd == Decimal(2)

    def test_price_improvement_reads_negative_rather_than_zero(self) -> None:
        """Flooring at zero would hide half the cost model's error."""
        weighted = weigh(Side.BUY, [lot("99", "2", expected="100")])

        assert weighted.slippage_usd == Decimal(-2)


class TestLongAndShortSigns:
    def test_a_long_closed_higher_makes_money(self) -> None:
        """(110 - 100) * 2 = +20."""
        pnl = position_pnl(side=Side.BUY, entries=[lot("100", "2")], exits=[lot("110", "2")])

        assert pnl.price_pnl_usd == Decimal(20)
        assert pnl.realized_pnl_usd == Decimal(20)

    def test_a_long_closed_lower_loses(self) -> None:
        pnl = position_pnl(side=Side.BUY, entries=[lot("100", "2")], exits=[lot("90", "2")])

        assert pnl.price_pnl_usd == Decimal(-20)

    def test_a_short_closed_lower_makes_money(self) -> None:
        """Sold at 100, bought back at 90: (100 - 90) * 2 = +20."""
        pnl = position_pnl(side=Side.SELL, entries=[lot("100", "2")], exits=[lot("90", "2")])

        assert pnl.price_pnl_usd == Decimal(20)

    def test_a_short_closed_higher_loses(self) -> None:
        pnl = position_pnl(side=Side.SELL, entries=[lot("100", "2")], exits=[lot("110", "2")])

        assert pnl.price_pnl_usd == Decimal(-20)

    def test_exposure_is_signed_by_the_entry_side(self) -> None:
        long_leg = position_pnl(side=Side.BUY, entries=[lot("100", "2")], exits=[])
        short_leg = position_pnl(side=Side.SELL, entries=[lot("100", "2")], exits=[])

        assert long_leg.gross_exposure_usd == Decimal(200)
        assert long_leg.net_exposure_usd == Decimal(200)
        assert short_leg.gross_exposure_usd == Decimal(200)
        assert short_leg.net_exposure_usd == Decimal(-200)


class TestWeightedPartialFills:
    def test_partial_fills_use_their_own_quantities_and_prices(self) -> None:
        """Entry (100*1 + 104*1)/2 = 102; exit (110*1 + 114*1)/2 = 112.

        Price P&L = (112 - 102) * 2 = +20. A naive implementation that used
        the first or the last fill price would report 20 or 20 by accident
        here, so the prices are chosen to differ: last-fill would give
        (114-104)*2 = 20 too, first-fill (110-100)*2 = 20. The weighted
        prices themselves are asserted below to pin it down.
        """
        pnl = position_pnl(
            side=Side.BUY,
            entries=[lot("100", "1"), lot("104", "1")],
            exits=[lot("110", "1"), lot("114", "1")],
        )

        assert pnl.entry.price == Decimal(102)
        assert pnl.exit.price == Decimal(112)
        assert pnl.price_pnl_usd == Decimal(20)

    def test_unequal_partial_fills_weight_by_size(self) -> None:
        """(100*1 + 200*3)/4 = 175, which no unweighted mean produces."""
        pnl = position_pnl(side=Side.BUY, entries=[lot("100", "1"), lot("200", "3")], exits=[])

        assert pnl.entry.price == Decimal(175)


class TestFeesChargedExactlyOnce:
    def test_entry_and_exit_fees_are_both_charged_on_a_full_close(self) -> None:
        """20 of price P&L, less 1 entry fee and 2 exit fee, is 17."""
        pnl = position_pnl(
            side=Side.BUY,
            entries=[lot("100", "2", "1")],
            exits=[lot("110", "2", "2")],
        )

        assert pnl.price_pnl_usd == Decimal(20)
        assert pnl.fees_on_closed_usd == Decimal(3)
        assert pnl.realized_pnl_usd == Decimal(17)

    def test_a_partial_close_prorates_the_entry_fee(self) -> None:
        """Half closed, so half the 4.00 entry fee: 2.00, plus its own 1.00."""
        pnl = position_pnl(
            side=Side.BUY,
            entries=[lot("100", "2", "4")],
            exits=[lot("110", "1", "1")],
        )

        assert pnl.fees_on_closed_usd == Decimal(3)
        # (110 - 100) * 1 = 10, less 3 of fees.
        assert pnl.realized_pnl_usd == Decimal(7)

    def test_successive_partial_closes_charge_the_entry_fee_exactly_once(self) -> None:
        """Two halves must total the whole fee, not more and not less."""
        first = position_pnl(
            side=Side.BUY, entries=[lot("100", "2", "4")], exits=[lot("110", "1", "1")]
        )
        both = position_pnl(
            side=Side.BUY,
            entries=[lot("100", "2", "4")],
            exits=[lot("110", "1", "1"), lot("112", "1", "1")],
        )
        second_charge = both.fees_on_closed_usd - first.fees_on_closed_usd

        assert first.fees_on_closed_usd + second_charge == Decimal(6)
        # 4 of entry fee, in total, plus 1 + 1 of exit fees.
        assert both.fees_on_closed_usd == Decimal(6)

    def test_lifetime_fees_are_reported_separately_from_the_closed_charge(self) -> None:
        pnl = position_pnl(
            side=Side.BUY, entries=[lot("100", "2", "4")], exits=[lot("110", "1", "1")]
        )

        assert pnl.fees_usd == Decimal(5)
        assert pnl.fees_on_closed_usd == Decimal(3)


class TestSlippageIsNotDoubleCounted:
    def test_realized_pnl_ignores_slippage_because_the_fills_already_paid_it(self) -> None:
        """Bought at 101 expecting 100, sold at 110 expecting 111.

        Slippage attribution is 1*2 + 1*2 = 4, and realized P&L is
        (110 - 101) * 2 = 18 with no fees. Subtracting the 4 again would
        report 14 for a round trip that made 18.
        """
        pnl = position_pnl(
            side=Side.BUY,
            entries=[lot("101", "2", expected="100")],
            exits=[lot("110", "2", expected="111")],
        )

        assert pnl.slippage_usd == Decimal(4)
        assert pnl.realized_pnl_usd == Decimal(18)


class TestUnmeasuredCashFlows:
    def test_funding_is_named_as_unmeasured_rather_than_assumed_zero(self) -> None:
        pnl = position_pnl(side=Side.BUY, entries=[lot("100", "1")], exits=[lot("100", "1")])

        assert pnl.funding_pnl_usd is None
        assert FUNDING in pnl.unmeasured
        assert not pnl.realized_is_complete

    def test_a_measured_funding_payment_enters_realized_pnl_signed(self) -> None:
        received = position_pnl(
            side=Side.SELL,
            entries=[lot("100", "1")],
            exits=[lot("100", "1")],
            funding_pnl_usd=Decimal("0.50"),
        )
        paid = position_pnl(
            side=Side.SELL,
            entries=[lot("100", "1")],
            exits=[lot("100", "1")],
            funding_pnl_usd=Decimal("-0.50"),
        )

        assert received.realized_pnl_usd == Decimal("0.50")
        assert paid.realized_pnl_usd == Decimal("-0.50")
        assert received.unmeasured == ()

    def test_only_a_borrowed_leg_is_missing_a_borrow_cost(self) -> None:
        """A long spot leg borrows nothing and is not missing anything."""
        borrowed = position_pnl(
            side=Side.SELL,
            entries=[lot("100", "1")],
            exits=[lot("100", "1")],
            funding_pnl_usd=Decimal(0),
            borrows=True,
        )
        owned = position_pnl(
            side=Side.BUY,
            entries=[lot("100", "1")],
            exits=[lot("100", "1")],
            funding_pnl_usd=Decimal(0),
            borrows=False,
        )

        assert borrowed.unmeasured == (SPOT_BORROW,)
        assert owned.unmeasured == ()

    def test_a_measured_borrow_cost_is_subtracted(self) -> None:
        pnl = position_pnl(
            side=Side.SELL,
            entries=[lot("100", "1")],
            exits=[lot("90", "1")],
            funding_pnl_usd=Decimal(0),
            borrow_cost_usd=Decimal("2"),
            borrows=True,
        )

        # 10 of price P&L, less 2 of borrow.
        assert pnl.realized_pnl_usd == Decimal(8)


class TestOpenAndPartiallyClosed:
    def test_an_open_position_realizes_nothing(self) -> None:
        pnl = position_pnl(side=Side.BUY, entries=[lot("100", "2", "1")], exits=[])

        assert pnl.realized_pnl_usd == Decimal(0)
        assert pnl.price_pnl_usd == Decimal(0)
        assert not pnl.is_flat

    def test_a_partially_closed_position_realizes_only_the_closed_part(self) -> None:
        pnl = position_pnl(side=Side.BUY, entries=[lot("100", "4")], exits=[lot("110", "1")])

        assert pnl.realized_pnl_usd == Decimal(10)
        assert pnl.open_quantity == Decimal(3)
        assert pnl.is_partially_closed
        assert not pnl.is_flat

    def test_unrealized_uses_the_mark_on_the_open_remainder_only(self) -> None:
        pnl = position_pnl(
            side=Side.BUY,
            entries=[lot("100", "4")],
            exits=[lot("110", "1")],
            mark_price=Decimal(105),
        )

        # (105 - 100) * 3 open units.
        assert pnl.unrealized_pnl_usd == Decimal(15)

    def test_a_missing_mark_leaves_unrealized_unknown_rather_than_zero(self) -> None:
        pnl = position_pnl(side=Side.BUY, entries=[lot("100", "4")], exits=[])

        assert pnl.unrealized_pnl_usd is None

    def test_a_flat_position_has_no_unrealized_pnl_and_no_exposure(self) -> None:
        pnl = position_pnl(side=Side.BUY, entries=[lot("100", "2")], exits=[lot("110", "2")])

        assert pnl.unrealized_pnl_usd == Decimal(0)
        assert pnl.gross_exposure_usd == Decimal(0)
        assert pnl.is_flat

    def test_closing_more_than_was_opened_is_refused(self) -> None:
        with pytest.raises(ValueError, match="may only reduce"):
            position_pnl(side=Side.BUY, entries=[lot("100", "1")], exits=[lot("100", "2")])

    def test_holding_duration_is_measured_only_once_flat(self) -> None:
        open_leg = position_pnl(
            side=Side.BUY, entries=[lot("100", "2")], exits=[lot("110", "1", minutes=30)]
        )
        flat = position_pnl(
            side=Side.BUY, entries=[lot("100", "2")], exits=[lot("110", "2", minutes=30)]
        )

        assert open_leg.holding is None
        assert flat.holding == timedelta(minutes=30)


def pair(buy_pnl: object, sell_pnl: object, *, closed: bool = True) -> PairedTrade:
    return PairedTrade(
        attempt_id="a1",
        strategy="spot_perp_basis",
        legs=(buy_pnl, sell_pnl),  # type: ignore[arg-type]
        opened_at=NOW,
        closed_at=NOW + timedelta(minutes=5) if closed else None,
    )


class TestPairedTrades:
    def test_a_hedged_pair_nets_its_two_legs(self) -> None:
        """The spot leg losing what the perpetual made is the intended result.

        Long spot 100 -> 98 loses 2; short perp 100 -> 98 makes 2. Net zero,
        which is one breakeven trade and not one win plus one loss.
        """
        trade = pair(
            position_pnl(side=Side.BUY, entries=[lot("100", "1")], exits=[lot("98", "1")]),
            position_pnl(side=Side.SELL, entries=[lot("100", "1")], exits=[lot("98", "1")]),
        )

        assert trade.realized_pnl_usd == Decimal(0)
        assert trade.outcome is TradeOutcome.BREAKEVEN

    def test_a_converging_basis_is_a_win_for_the_pair(self) -> None:
        """Bought at 100, sold at 101; closed at 102 and 102. +2 - 1 = +1."""
        trade = pair(
            position_pnl(side=Side.BUY, entries=[lot("100", "1")], exits=[lot("102", "1")]),
            position_pnl(side=Side.SELL, entries=[lot("101", "1")], exits=[lot("102", "1")]),
        )

        assert trade.realized_pnl_usd == Decimal(1)
        assert trade.outcome is TradeOutcome.WIN

    def test_fees_can_turn_a_converged_basis_into_a_loss(self) -> None:
        """The measured reality: the round trip has to clear its own fees."""
        trade = pair(
            position_pnl(
                side=Side.BUY, entries=[lot("100", "1", "1")], exits=[lot("102", "1", "1")]
            ),
            position_pnl(
                side=Side.SELL, entries=[lot("101", "1", "1")], exits=[lot("102", "1", "1")]
            ),
        )

        # +1 of price P&L against 4 of fees.
        assert trade.realized_pnl_usd == Decimal(-3)
        assert trade.outcome is TradeOutcome.LOSS

    def test_an_incomplete_pair_has_no_outcome_at_all(self) -> None:
        trade = pair(
            position_pnl(side=Side.BUY, entries=[lot("100", "1")], exits=[lot("102", "1")]),
            position_pnl(side=Side.SELL, entries=[lot("101", "1")], exits=[]),
            closed=False,
        )

        assert not trade.is_complete
        assert trade.outcome is None

    def test_one_flat_leg_beside_a_live_one_is_unpaired(self) -> None:
        trade = pair(
            position_pnl(side=Side.BUY, entries=[lot("100", "1")], exits=[lot("102", "1")]),
            position_pnl(side=Side.SELL, entries=[lot("101", "1")], exits=[]),
            closed=False,
        )

        assert trade.is_unpaired

    def test_two_live_legs_are_not_unpaired(self) -> None:
        trade = pair(
            position_pnl(side=Side.BUY, entries=[lot("100", "1")], exits=[]),
            position_pnl(side=Side.SELL, entries=[lot("101", "1")], exits=[]),
            closed=False,
        )

        assert not trade.is_unpaired

    def test_unequal_live_quantities_are_unpaired(self) -> None:
        trade = pair(
            position_pnl(side=Side.BUY, entries=[lot("100", "1")], exits=[]),
            position_pnl(side=Side.SELL, entries=[lot("101", "0.5")], exits=[]),
            closed=False,
        )

        assert trade.is_unpaired

    def test_one_leg_is_never_a_complete_paired_trade(self) -> None:
        leg = position_pnl(side=Side.BUY, entries=[lot("100", "1")], exits=[lot("101", "1")])
        trade = PairedTrade("a1", "spot_perp_basis", (leg,), NOW, NOW)

        assert not trade.is_complete
        assert trade.outcome is None

    def test_an_unmarkable_leg_makes_the_pair_unrealized_unknown(self) -> None:
        trade = pair(
            position_pnl(
                side=Side.BUY, entries=[lot("100", "1")], exits=[], mark_price=Decimal(101)
            ),
            position_pnl(side=Side.SELL, entries=[lot("101", "1")], exits=[]),
            closed=False,
        )

        assert trade.unrealized_pnl_usd is None


class TestStatistics:
    def test_no_trades_produces_nulls_not_zeroes(self) -> None:
        statistics = summarise_trades([])

        assert statistics.win_rate is None
        assert statistics.profit_factor is None
        assert statistics.expectancy_usd is None
        assert statistics.average_trade_usd is None

    def test_counts_split_wins_losses_and_breakevens(self) -> None:
        statistics = summarise_trades([Decimal(10), Decimal(-4), Decimal(0), Decimal(6)])

        assert statistics.trade_count == 4
        assert statistics.winning_trades == 2
        assert statistics.losing_trades == 1
        assert statistics.breakeven_trades == 1

    def test_averages_and_ratios_are_hand_checkable(self) -> None:
        """Wins 10 and 6, loss -4, one breakeven.

        net 12 over 4 trades -> average 3; average win 8; average loss -4;
        win rate 0.5; profit factor 16/4 = 4.
        """
        statistics = summarise_trades([Decimal(10), Decimal(-4), Decimal(0), Decimal(6)])

        assert statistics.average_trade_usd == Decimal(3)
        assert statistics.average_win_usd == Decimal(8)
        assert statistics.average_loss_usd == Decimal(-4)
        assert statistics.win_rate == 0.5
        assert statistics.profit_factor == 4.0
        assert statistics.expectancy_usd == Decimal(3)

    def test_profit_factor_is_null_without_a_losing_trade(self) -> None:
        """Infinity would read as a measured edge rather than an untested one."""
        statistics = summarise_trades([Decimal(10), Decimal(6)])

        assert statistics.profit_factor is None
        assert statistics.win_rate == 1.0


class TestDrawdownAndReturn:
    def test_drawdown_is_the_largest_peak_to_trough_fall(self) -> None:
        """100 -> 120 -> 90 -> 130 -> 110: worst fall is 120 to 90 = 30."""
        curve = [Decimal(100), Decimal(120), Decimal(90), Decimal(130), Decimal(110)]

        assert max_drawdown(curve) == Decimal(30)

    def test_one_point_cannot_describe_a_decline(self) -> None:
        assert max_drawdown([Decimal(100)]) is None

    def test_a_rising_curve_has_no_drawdown(self) -> None:
        assert max_drawdown([Decimal(100), Decimal(110)]) == Decimal(0)

    def test_total_return_is_first_to_last(self) -> None:
        assert total_return_pct([Decimal(100), Decimal(90), Decimal(110)]) == 10.0

    def test_total_return_needs_a_positive_starting_equity(self) -> None:
        assert total_return_pct([Decimal(0), Decimal(110)]) is None
        assert total_return_pct([Decimal(100)]) is None


def curve(count: int, step: float, *, interval: timedelta = timedelta(minutes=1)):  # type: ignore[no-untyped-def]
    points = []
    equity = Decimal(1000)
    for index in range(count):
        points.append((NOW + interval * index, equity))
        equity = equity * (Decimal(1) + Decimal(str(step)))
    return points


class TestReturnSampling:
    def test_regular_points_become_a_sample_with_its_interval(self) -> None:
        sample = sample_returns(curve(5, 0.01), interval=timedelta(minutes=1))

        assert sample is not None
        assert len(sample.returns) == 4
        assert sample.periods_per_year == pytest.approx(365 * 24 * 60)

    def test_irregular_spacing_is_refused_rather_than_annualised(self) -> None:
        """Event-level observations are not a fixed-interval return series."""
        points = [
            (NOW, Decimal(1000)),
            (NOW + timedelta(minutes=1), Decimal(1010)),
            (NOW + timedelta(minutes=47), Decimal(1020)),
        ]

        assert sample_returns(points, interval=timedelta(minutes=1)) is None

    def test_one_point_is_not_a_series(self) -> None:
        assert sample_returns(curve(1, 0.01), interval=timedelta(minutes=1)) is None

    def test_non_positive_equity_cannot_produce_a_return(self) -> None:
        points = [(NOW, Decimal(0)), (NOW + timedelta(minutes=1), Decimal(10))]

        assert sample_returns(points, interval=timedelta(minutes=1)) is None


class TestRatios:
    def test_sharpe_is_null_below_the_minimum_observation_count(self) -> None:
        sample = sample_returns(curve(10, 0.01), interval=timedelta(minutes=1))
        assert sample is not None

        assert sharpe_ratio(sample, minimum=30) is None
        assert sortino_ratio(sample, minimum=30) is None

    def test_a_constant_return_has_no_deviation_to_divide_by(self) -> None:
        sample = sample_returns(curve(40, 0.01), interval=timedelta(minutes=1))
        assert sample is not None

        assert sharpe_ratio(sample, minimum=30) is None

    def test_sortino_is_null_when_nothing_fell(self) -> None:
        points = [(NOW + timedelta(minutes=i), Decimal(1000 + i * 10)) for i in range(40)]
        sample = sample_returns(points, interval=timedelta(minutes=1))
        assert sample is not None

        assert sortino_ratio(sample, minimum=30) is None

    def test_a_mixed_series_produces_both_ratios(self) -> None:
        equity = Decimal(1000)
        points = []
        for index in range(41):
            points.append((NOW + timedelta(minutes=index), equity))
            equity += Decimal(10) if index % 2 == 0 else Decimal(-5)
        sample = sample_returns(points, interval=timedelta(minutes=1))
        assert sample is not None

        sharpe = sharpe_ratio(sample, minimum=30)
        sortino = sortino_ratio(sample, minimum=30)

        assert sharpe is not None and sortino is not None
        # Sortino only penalises downside, so it exceeds Sharpe on a series
        # whose upside moves are the larger ones.
        assert sortino > sharpe

    def test_annualisation_follows_the_sample_interval(self) -> None:
        """The same returns sampled hourly and daily cannot share a Sharpe."""
        equity = Decimal(1000)
        values = []
        for index in range(41):
            values.append(equity)
            equity += Decimal(10) if index % 2 == 0 else Decimal(-5)
        hourly = sample_returns(
            [(NOW + timedelta(hours=i), value) for i, value in enumerate(values)],
            interval=timedelta(hours=1),
        )
        daily = sample_returns(
            [(NOW + timedelta(days=i), value) for i, value in enumerate(values)],
            interval=timedelta(days=1),
        )
        assert hourly is not None and daily is not None

        hourly_sharpe = sharpe_ratio(hourly, minimum=30)
        daily_sharpe = sharpe_ratio(daily, minimum=30)
        assert hourly_sharpe is not None and daily_sharpe is not None

        assert hourly_sharpe == pytest.approx(daily_sharpe * (24**0.5))
