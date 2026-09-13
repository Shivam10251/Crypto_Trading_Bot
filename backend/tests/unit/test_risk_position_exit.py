"""Reduce-only, and the deliberate difference between closing and opening.

Two things are being pinned here. First, every way a "close" could fail to be
one - wrong side, too much size, a position that is already flat, a shadow
probe - is refused before an order exists. Second, the kill switch does *not*
refuse a close: a halt stops new exposure, and a close removes it, so gating
one on the other would leave the account holding exactly the risk the halt was
called for.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from tests.unit.test_risk_engine import FakeStore, build_engine
from trading_bot.db.models.enums import (
    ExecutionMode,
    MarketType,
    PositionStatus,
    RiskDecision,
    RiskEventType,
    Side,
)
from trading_bot.exchange.models import MarketRef
from trading_bot.risk.position_exit import ExitLeg, ExitRequest, check_reduce_only

NOW = datetime(2026, 9, 12, 12, 0, tzinfo=UTC)
SPOT = MarketRef("binance", "BTCUSDT", MarketType.SPOT)
PERP = MarketRef("binance", "BTCUSDT", MarketType.PERPETUAL)


def leg(
    *,
    entry_side: Side = Side.BUY,
    close_side: Side | None = None,
    quantity: str = "1",
    open_quantity: str = "1",
    status: PositionStatus = PositionStatus.OPEN,
    is_shadow: bool = False,
    position_id: int = 1,
) -> ExitLeg:
    return ExitLeg(
        position_id=position_id,
        ref=SPOT,
        entry_side=entry_side,
        close_side=close_side or (Side.SELL if entry_side is Side.BUY else Side.BUY),
        quantity=Decimal(quantity),
        open_quantity=Decimal(open_quantity),
        status=status,
        is_shadow=is_shadow,
    )


class TestReduceOnly:
    def test_the_opposite_side_for_the_open_quantity_is_admitted(self) -> None:
        assert check_reduce_only([leg()]) is None

    def test_closing_a_short_needs_a_buy(self) -> None:
        assert check_reduce_only([leg(entry_side=Side.SELL)]) is None

    def test_the_same_side_would_increase_the_position(self) -> None:
        violation = check_reduce_only([leg(entry_side=Side.BUY, close_side=Side.BUY)])

        assert violation is not None
        assert "would increase it" in violation.reason

    def test_more_than_the_open_quantity_would_reverse_it(self) -> None:
        violation = check_reduce_only([leg(quantity="2", open_quantity="1")])

        assert violation is not None
        assert "reverse the position" in violation.reason

    def test_a_zero_quantity_close_is_refused(self) -> None:
        violation = check_reduce_only([leg(quantity="0")])

        assert violation is not None
        assert "not positive" in violation.reason

    def test_a_position_with_nothing_open_cannot_be_closed(self) -> None:
        violation = check_reduce_only([leg(open_quantity="0")])

        assert violation is not None
        assert "no open quantity" in violation.reason

    def test_an_already_closed_position_is_refused(self) -> None:
        violation = check_reduce_only([leg(status=PositionStatus.CLOSED)])

        assert violation is not None
        assert "already closed" in violation.reason

    def test_a_liquidated_position_cannot_be_closed_again(self) -> None:
        violation = check_reduce_only([leg(status=PositionStatus.LIQUIDATED)])

        assert violation is not None
        assert "liquidated" in violation.reason

    def test_a_position_from_another_mode_is_refused(self) -> None:
        violation = check_reduce_only(
            [leg()],
            mode=ExecutionMode.LIVE,
        )

        assert violation is not None
        assert "does not match" in violation.reason

    def test_a_shadow_probe_is_never_closed(self) -> None:
        """Its exposure is hypothetical; closing it would place a real order."""
        violation = check_reduce_only([leg(is_shadow=True)])

        assert violation is not None
        assert "hypothetical" in violation.reason

    def test_a_close_must_name_at_least_one_position(self) -> None:
        violation = check_reduce_only([])

        assert violation is not None

    def test_one_bad_leg_refuses_the_whole_close(self) -> None:
        violation = check_reduce_only([leg(position_id=1), leg(position_id=2, quantity="5")])

        assert violation is not None
        assert violation.position_id == 2


def request(**overrides: object) -> ExitRequest:
    legs = overrides.pop("legs", (leg(position_id=1), leg(position_id=2, entry_side=Side.SELL)))
    defaults: dict[str, object] = {
        "attempt_id": "a1",
        "intent_id": "close:a1:0",
        "strategy": "spot_perp_basis",
        "reason": "BASIS_CONVERGED",
        "legs": legs,
        "policy_context": {"remaining_bps": "0"},
    }
    return ExitRequest(**{**defaults, **overrides})  # type: ignore[arg-type]


class TestExitDecisions:
    async def test_an_admitted_close_writes_a_durable_position_exit_row(self) -> None:
        store = FakeStore()
        risk, _, _ = await build_engine(store=store)

        verdict = await risk.evaluate_exit(request())

        assert verdict.is_approved
        assert verdict.draft.event_type is RiskEventType.POSITION_EXIT
        assert verdict.draft.decision is RiskDecision.APPROVED
        assert verdict.draft.context is not None
        assert verdict.draft.context["exit_reason"] == "BASIS_CONVERGED"
        # The policy's own arithmetic travels onto the row.
        assert verdict.draft.context["remaining_bps"] == "0"

    async def test_a_reduce_only_violation_is_refused_and_recorded(self) -> None:
        store = FakeStore()
        risk, _, _ = await build_engine(store=store)

        verdict = await risk.evaluate_exit(request(legs=(leg(quantity="9"),)))

        assert not verdict.is_approved
        assert verdict.draft.event_type is RiskEventType.REDUCE_ONLY_VIOLATION
        assert verdict.draft.decision is RiskDecision.REJECTED

    async def test_a_halt_does_not_refuse_a_close(self) -> None:
        """A kill stops new exposure. Refusing to reduce it would keep the
        very risk the halt was called for."""
        store = FakeStore()
        risk, _, _ = await build_engine(store=store)
        await risk._kill_switch.trigger(who="operator", reason="stop everything")

        verdict = await risk.evaluate_exit(request())

        assert verdict.is_approved

    async def test_a_close_during_a_halt_records_that_it_was_halted(self) -> None:
        store = FakeStore()
        risk, _, _ = await build_engine(store=store)
        await risk._kill_switch.trigger(who="operator", reason="stop everything")

        verdict = await risk.evaluate_exit(request())

        assert verdict.draft.context is not None
        assert "stop everything" in str(verdict.draft.context["kill_switch_engaged"])

    async def test_a_close_that_cannot_be_recorded_is_not_approved(self) -> None:
        """A database that cannot store the decision cannot store the close's
        own orders and fills either, and an unrecorded close is exposure the
        system believes it still has."""
        store = FakeStore(fail=True)
        risk, _, _ = await build_engine(store=store)

        verdict = await risk.evaluate_exit(request())

        assert not verdict.is_approved
        assert not verdict.is_durable

    async def test_a_retried_close_converges_on_one_row(self) -> None:
        store = FakeStore()
        risk, _, _ = await build_engine(store=store)

        first = await risk.evaluate_exit(request())
        second = await risk.evaluate_exit(request())

        assert first.risk_event_id == second.risk_event_id
        assert len(store.rows) == 1

    @pytest.mark.parametrize("reason", ["UNPAIRED_RESIDUAL", "ADVERSE_BASIS"])
    async def test_the_reason_is_carried_as_the_limit_name(self, reason: str) -> None:
        store = FakeStore()
        risk, _, _ = await build_engine(store=store)

        verdict = await risk.evaluate_exit(request(reason=reason))

        assert verdict.draft.limit_name == reason
