"""The exit policy: when to stop holding, and when to refuse to decide.

The policy is pure, so these tests are arithmetic. The cases that matter are
the ones where it must *not* fire - an unpriceable book, depth that cannot
fill the residual - and the ones where a risk-reducing reason overrides all
of that.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from trading_bot.core.config import ExitPolicyConfig
from trading_bot.db.models.enums import MarketType, Side
from trading_bot.exchange.models import MarketRef
from trading_bot.portfolio.exits import (
    RISK_REDUCING,
    BasisView,
    ExitDeferral,
    ExitReason,
    basis_bps,
    evaluate,
)
from trading_bot.portfolio.valuation import ExecutableExit

NOW = datetime(2026, 9, 12, 12, 0, tzinfo=UTC)
SPOT = MarketRef("binance", "BTCUSDT", MarketType.SPOT)
PERP = MarketRef("binance", "BTCUSDT", MarketType.PERPETUAL)
CONFIG = ExitPolicyConfig(target_basis_bps=1.0, max_holding_minutes=60, adverse_basis_bps=25.0)


def priced(
    ref: MarketRef,
    price: str | None,
    *,
    entry_side: Side,
    complete: bool = True,
    problem: str | None = None,
) -> ExecutableExit:
    return ExecutableExit(
        ref=ref,
        side=Side.SELL if entry_side is Side.BUY else Side.BUY,
        quantity=Decimal(1),
        price=Decimal(price) if price is not None else None,
        fillable=Decimal(1) if complete else Decimal("0.4"),
        complete=complete,
        problem=problem,
    )


def view(
    *,
    buy_entry: str = "100000",
    sell_entry: str = "100100",
    buy_exit: str | None = "100000",
    sell_exit: str | None = "100000",
    minutes: int = 5,
    unpaired: bool = False,
    complete: bool = True,
    attempts: int = 0,
) -> BasisView:
    return BasisView(
        attempt_id="a1",
        opened_at=NOW - timedelta(minutes=minutes),
        buy_entry_price=Decimal(buy_entry),
        sell_entry_price=Decimal(sell_entry),
        buy_exit=priced(SPOT, buy_exit, entry_side=Side.BUY, complete=complete),
        sell_exit=priced(PERP, sell_exit, entry_side=Side.SELL, complete=complete),
        is_unpaired=unpaired,
        close_attempts=attempts,
    )


class TestBasisArithmetic:
    def test_the_basis_is_the_sold_leg_over_the_bought_one(self) -> None:
        """(100100 - 100000) / 100000 = 10 bps."""
        assert basis_bps(Decimal(100100), Decimal(100000), Decimal(100000)) == Decimal(10)

    def test_the_reference_is_fixed_so_entry_and_exit_compare(self) -> None:
        entry = basis_bps(Decimal(100100), Decimal(100000), Decimal(100000))
        exit_basis = basis_bps(Decimal(100010), Decimal(100000), Decimal(100000))

        assert entry - exit_basis == Decimal(9)


class TestConvergence:
    def test_a_converged_basis_closes(self) -> None:
        """Entered at 10 bps; both legs now at 100000, so 0 bps remain."""
        decision = evaluate(view(), CONFIG, NOW)

        assert decision.should_close
        assert decision.reason is ExitReason.BASIS_CONVERGED
        assert decision.remaining_bps == Decimal(0)
        assert decision.captured_bps == Decimal(10)

    def test_a_basis_still_open_does_not_close(self) -> None:
        """Sold leg still 8 bps above the bought one, target is 1 bp."""
        decision = evaluate(view(sell_exit="100080"), CONFIG, NOW)

        assert not decision.should_close
        assert decision.reason is None
        assert decision.remaining_bps == Decimal(8)

    def test_convergence_in_the_other_direction_works_the_same(self) -> None:
        """Entered with the sold leg BELOW the bought one: -10 bps."""
        decision = evaluate(view(sell_entry="99900"), CONFIG, NOW)

        assert decision.should_close
        assert decision.reason is ExitReason.BASIS_CONVERGED
        assert decision.entry_basis_bps == Decimal(-10)
        assert decision.remaining_bps == Decimal(0)

    def test_a_converged_target_waits_when_depth_cannot_fill_it(self) -> None:
        """A partial exit at a worse price is not what the target measured."""
        decision = evaluate(view(complete=False), CONFIG, NOW)

        assert not decision.should_close
        assert decision.reason is ExitReason.BASIS_CONVERGED
        assert decision.deferral is ExitDeferral.INCOMPLETE_DEPTH


class TestStops:
    def test_a_basis_widening_against_us_stops_out(self) -> None:
        """Entered at 10 bps, now 40 bps: widened 30, past the 25 bps stop."""
        decision = evaluate(view(sell_exit="100400"), CONFIG, NOW)

        assert decision.should_close
        assert decision.reason is ExitReason.ADVERSE_BASIS

    def test_the_stop_does_not_claim_the_exit_is_profitable(self) -> None:
        decision = evaluate(view(sell_exit="100400"), CONFIG, NOW)

        assert decision.captured_bps is not None
        assert decision.captured_bps < 0

    def test_the_holding_limit_closes_a_position_going_nowhere(self) -> None:
        decision = evaluate(view(sell_exit="100080", minutes=120), CONFIG, NOW)

        assert decision.should_close
        assert decision.reason is ExitReason.MAX_HOLDING_PERIOD

    def test_an_adverse_basis_outranks_the_holding_limit(self) -> None:
        decision = evaluate(view(sell_exit="100400", minutes=120), CONFIG, NOW)

        assert decision.reason is ExitReason.ADVERSE_BASIS


class TestResidualExposure:
    def test_an_unpaired_attempt_closes_before_anything_else_is_considered(self) -> None:
        decision = evaluate(view(sell_exit="100080", unpaired=True), CONFIG, NOW)

        assert decision.should_close
        assert decision.reason is ExitReason.UNPAIRED_RESIDUAL

    def test_residual_exposure_is_closed_even_with_no_price_at_all(self) -> None:
        decision = evaluate(view(buy_exit=None, sell_exit=None, unpaired=True), CONFIG, NOW)

        assert decision.should_close
        assert decision.reason is ExitReason.UNPAIRED_RESIDUAL

    def test_the_risk_reducing_reasons_are_named_as_a_set(self) -> None:
        assert ExitReason.BASIS_CONVERGED not in RISK_REDUCING
        assert {
            ExitReason.UNPAIRED_RESIDUAL,
            ExitReason.ADVERSE_BASIS,
            ExitReason.MAX_HOLDING_PERIOD,
        } == RISK_REDUCING


class TestUnpriceable:
    def test_an_unpriceable_exit_defers_rather_than_guessing(self) -> None:
        decision = evaluate(view(buy_exit=None), CONFIG, NOW)

        assert not decision.should_close
        assert decision.deferral is ExitDeferral.UNPRICEABLE

    def test_the_holding_limit_still_fires_without_a_price(self) -> None:
        """The one condition that needs no price must not be blocked by one."""
        decision = evaluate(view(buy_exit=None, minutes=120), CONFIG, NOW)

        assert decision.should_close
        assert decision.reason is ExitReason.MAX_HOLDING_PERIOD

    def test_a_missing_leg_defers_until_the_holding_limit(self) -> None:
        without_leg = BasisView(
            attempt_id="a1",
            opened_at=NOW - timedelta(minutes=5),
            buy_entry_price=Decimal(100000),
            sell_entry_price=None,
            buy_exit=priced(SPOT, "100000", entry_side=Side.BUY),
            sell_exit=None,
            is_unpaired=False,
        )

        assert evaluate(without_leg, CONFIG, NOW).deferral is ExitDeferral.UNPRICEABLE


class TestAttemptsExhausted:
    def test_a_close_that_keeps_failing_stops_being_retried(self) -> None:
        decision = evaluate(view(attempts=5), CONFIG, NOW)

        assert decision.reason is ExitReason.BASIS_CONVERGED
        assert decision.deferral is ExitDeferral.ATTEMPTS_EXHAUSTED
        assert not decision.should_close

    def test_exhaustion_also_stops_a_residual_close(self) -> None:
        """Even a risk-reducing exit stops: an operator has to look at it."""
        decision = evaluate(view(unpaired=True, attempts=9), CONFIG, NOW)

        assert decision.deferral is ExitDeferral.ATTEMPTS_EXHAUSTED


class TestAuditContext:
    def test_every_number_behind_a_decision_is_serialisable(self) -> None:
        context = evaluate(view(), CONFIG, NOW).context()

        assert context["exit_reason"] == "BASIS_CONVERGED"
        assert Decimal(str(context["entry_basis_bps"])) == Decimal(10)
        assert Decimal(str(context["remaining_bps"])) == Decimal(0)
        assert context["holding_seconds"] == 300
        assert context["deferral"] is None
