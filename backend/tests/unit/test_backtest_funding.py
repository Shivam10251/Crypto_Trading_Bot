"""Funding attributed from recorded settlements - or honestly left unmeasured."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from trading_bot.backtest.funding import attribute_funding, attribute_funding_until
from trading_bot.backtest.market_state import SettlementObservation
from trading_bot.db.models.enums import Side
from trading_bot.portfolio.accounting import FillLot

SETTLE = datetime(2026, 9, 1, 8, tzinfo=UTC)
AGE = timedelta(minutes=2)


def lot(quantity: str, at: datetime) -> FillLot:
    return FillLot(
        price=Decimal(100_000), quantity=Decimal(quantity), fee_usd=Decimal(0), filled_at=at
    )


def observed(
    settles_at: datetime, *, before: timedelta, rate: str, mark: str = "100000", hours: int = 8
) -> SettlementObservation:
    return SettlementObservation(
        settles_at=settles_at,
        observed_at=settles_at - before,
        rate=Decimal(rate),
        mark_price=Decimal(mark),
        interval_hours=hours,
    )


def attribute(
    side: Side, entries: list[FillLot], exits: list[FillLot], *observations: SettlementObservation
):  # type: ignore[no-untyped-def]
    return attribute_funding(
        side=side,
        entries=entries,
        exits=exits,
        observations={item.settles_at: item for item in observations},
        max_observation_age=AGE,
    )


class TestSigns:
    def test_a_short_receives_a_positive_rate(self) -> None:
        result = attribute(
            Side.SELL,
            [lot("0.01", SETTLE - timedelta(minutes=5))],
            [lot("0.01", SETTLE + timedelta(minutes=5))],
            observed(SETTLE, before=timedelta(seconds=30), rate="0.0001", mark="100100"),
        )
        # 0.01 x 100,100 x 0.0001 = 0.1001, received.
        assert result.amount_usd == Decimal("0.100100")
        assert result.settlements == 1

    def test_a_long_pays_a_positive_rate_and_receives_a_negative_one(self) -> None:
        entries = [lot("2", SETTLE - timedelta(hours=1))]
        exits = [lot("2", SETTLE + timedelta(hours=8, minutes=1))]
        second = SETTLE + timedelta(hours=8)
        result = attribute(
            Side.BUY,
            entries,
            exits,
            observed(SETTLE, before=timedelta(seconds=10), rate="0.0002", mark="50"),
            observed(second, before=timedelta(seconds=10), rate="-0.0001", mark="40"),
        )
        # -(2 x 50 x 0.0002) + (2 x 40 x 0.0001) = -0.02 + 0.008
        assert result.amount_usd == Decimal("-0.012")
        assert result.settlements == 2

    def test_only_the_size_held_at_the_settlement_counts(self) -> None:
        entries = [
            lot("1", SETTLE - timedelta(minutes=10)),
            lot("1", SETTLE + timedelta(minutes=1)),
        ]
        exits = [lot("2", SETTLE + timedelta(minutes=2))]
        result = attribute(
            Side.SELL,
            entries,
            exits,
            observed(SETTLE, before=timedelta(seconds=5), rate="0.001", mark="10"),
        )
        assert result.amount_usd == Decimal("0.010")

    def test_a_holding_that_crosses_no_settlement_measured_zero(self) -> None:
        result = attribute(
            Side.SELL,
            [lot("1", SETTLE - timedelta(minutes=10))],
            [lot("1", SETTLE - timedelta(minutes=1))],
            # Observed during the holding, so the schedule is known; it is
            # far from the settlement, which does not matter - none was crossed.
            observed(SETTLE, before=timedelta(minutes=15), rate="0.001"),
        )
        assert result.amount_usd == Decimal(0)
        assert result.settlements == 0

    def test_an_open_leg_is_attributed_through_the_current_instant(self) -> None:
        observation = observed(SETTLE, before=timedelta(seconds=30), rate="0.0001", mark="100100")
        result = attribute_funding_until(
            side=Side.SELL,
            entries=[lot("0.01", SETTLE - timedelta(minutes=5))],
            exits=[],
            observations={SETTLE: observation},
            max_observation_age=AGE,
            until=SETTLE + timedelta(minutes=5),
        )
        assert result.amount_usd == Decimal("0.100100")
        assert result.settlements == 1


class TestUnmeasured:
    def test_no_observation_close_enough_before_the_settlement(self) -> None:
        result = attribute(
            Side.SELL,
            [lot("1", SETTLE - timedelta(minutes=10))],
            [lot("1", SETTLE + timedelta(minutes=10))],
            observed(SETTLE, before=timedelta(minutes=3), rate="0.001"),
        )
        assert result.amount_usd is None
        assert "no observation within" in (result.reason or "")

    def test_a_fill_at_the_settlement_instant_cannot_be_placed(self) -> None:
        result = attribute(
            Side.SELL,
            [lot("1", SETTLE - timedelta(minutes=10))],
            [lot("1", SETTLE)],
            observed(SETTLE, before=timedelta(seconds=5), rate="0.001"),
        )
        assert result.amount_usd is None

    def test_a_changed_interval_leaves_the_schedule_ambiguous(self) -> None:
        opened = SETTLE - timedelta(hours=1)
        result = attribute(
            Side.SELL,
            [lot("1", opened)],
            [lot("1", SETTLE + timedelta(hours=2))],
            observed(SETTLE, before=timedelta(seconds=5), rate="0.001", hours=8),
            observed(SETTLE + timedelta(hours=4), before=timedelta(hours=3), rate="0.001", hours=4),
        )
        assert result.amount_usd is None

    def test_no_schedule_at_all(self) -> None:
        result = attribute(Side.SELL, [lot("1", SETTLE)], [lot("1", SETTLE + timedelta(hours=9))])
        assert result.amount_usd is None

    def test_a_leg_that_is_not_flat_is_not_attributed(self) -> None:
        result = attribute(Side.SELL, [lot("1", SETTLE)], [])
        assert result.amount_usd is None


def test_a_holding_with_no_schedule_observed_by_its_close_is_unmeasured() -> None:
    """An observation received after the close cannot describe the holding."""
    result = attribute(
        Side.SELL,
        [lot("1", SETTLE - timedelta(minutes=10))],
        [lot("1", SETTLE - timedelta(minutes=1))],
        observed(SETTLE, before=timedelta(seconds=5), rate="0.001"),
    )
    assert result.amount_usd is None


class TestSettlementsCrossedWhileOpen:
    def test_counts_settlements_on_the_recorded_schedule_or_admits_it_does_not_know(
        self,
    ) -> None:
        from trading_bot.backtest.funding import settlements_crossed
        from trading_bot.backtest.market_state import SettlementObservation

        settles = datetime(2026, 9, 1, 8, tzinfo=UTC)
        observed = {
            settles: SettlementObservation(
                settles, settles - timedelta(minutes=1), Decimal("0.0001"), Decimal(1), 8
            )
        }
        before = settles - timedelta(minutes=30)
        assert settlements_crossed(observed, before, settles - timedelta(seconds=1)) == 0
        assert settlements_crossed(observed, before, settles) == 1
        assert settlements_crossed(observed, before, settles + timedelta(hours=16)) == 3
        assert settlements_crossed({}, before, settles) is None
        unpublished = {
            settles: SettlementObservation(
                settles, settles - timedelta(minutes=1), Decimal("0.0001"), Decimal(1), None
            )
        }
        assert settlements_crossed(unpublished, before, settles) is None
