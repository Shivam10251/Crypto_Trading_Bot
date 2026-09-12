"""Paper execution cannot assume infinite balances, margin or exposure."""

from __future__ import annotations

from tests.unit.test_execution_coordinator import Adapter, opportunity, signal
from trading_bot.core.config import ExecutionConfig, RiskConfig
from trading_bot.execution.account import AccountRejection, PaperAccount
from trading_bot.execution.coordinator import ExecutionCoordinator
from trading_bot.execution.models import RejectionCode


async def test_cash_and_margin_are_reserved_before_either_leg_submits() -> None:
    adapter = Adapter()
    account = PaperAccount(ExecutionConfig(paper_cash_usd=50), RiskConfig())
    attempt = await ExecutionCoordinator(adapter, account=account).execute(signal())
    assert attempt is not None
    assert adapter.submitted == []
    assert all(
        outcome.result.rejection is RejectionCode.INSUFFICIENT_MARGIN for outcome in attempt.legs
    )


async def test_open_fills_reduce_the_remaining_gross_exposure_capacity() -> None:
    adapter = Adapter()
    risk = RiskConfig(
        max_order_notional_usd=200,
        max_position_notional_usd=300,
        max_total_exposure_usd=300,
    )
    account = PaperAccount(ExecutionConfig(), risk)
    coordinator = ExecutionCoordinator(adapter, account=account)
    first = await coordinator.execute(signal(), execution_intent_id="first")
    second = await coordinator.execute(signal(), execution_intent_id="second")
    assert first is not None and first.is_hedged
    assert second is not None
    assert all(outcome.result.rejection is RejectionCode.EXPOSURE_LIMIT for outcome in second.legs)
    assert account.gross_exposure_usd > 0


async def test_open_fills_reduce_each_markets_position_capacity() -> None:
    adapter = Adapter()
    risk = RiskConfig(
        max_order_notional_usd=150,
        max_position_notional_usd=150,
        max_total_exposure_usd=1000,
    )
    account = PaperAccount(ExecutionConfig(), risk)
    coordinator = ExecutionCoordinator(adapter, account=account)

    first = await coordinator.execute(signal(), execution_intent_id="position:first")
    second = await coordinator.execute(signal(), execution_intent_id="position:second")

    assert first is not None and first.is_hedged
    assert second is not None
    assert all(outcome.result.rejection is RejectionCode.EXPOSURE_LIMIT for outcome in second.legs)


async def test_spot_short_needs_an_explicit_borrow_facility() -> None:
    adapter = Adapter()
    account = PaperAccount(ExecutionConfig(), RiskConfig())
    coordinator = ExecutionCoordinator(adapter, allow_spot_short=True, account=account)
    attempt = await coordinator.execute(signal(opportunity(perp_rich=False)))
    assert attempt is not None
    assert adapter.submitted == []
    assert all(
        outcome.result.rejection is RejectionCode.BORROW_UNAVAILABLE for outcome in attempt.legs
    )


async def test_shadow_probe_checks_but_does_not_consume_the_paper_portfolio() -> None:
    adapter = Adapter()
    account = PaperAccount(ExecutionConfig(), RiskConfig())
    coordinator = ExecutionCoordinator(
        adapter, account=account, shadow_account=PaperAccount(ExecutionConfig(), RiskConfig())
    )
    before_cash = account.cash_usd

    first = await coordinator.execute(signal(), is_shadow=True, execution_intent_id="shadow:1")
    second = await coordinator.execute(signal(), is_shadow=True, execution_intent_id="shadow:2")

    assert first is not None and second is not None
    assert first.is_hedged and second.is_hedged
    assert account.cash_usd == before_cash
    assert account.gross_exposure_usd == 0


async def test_a_probe_reserves_against_its_own_ledger_not_the_trading_one() -> None:
    """Defect 9: the two ledgers are separate objects, not one shared budget."""
    risk = RiskConfig(
        max_order_notional_usd=200, max_position_notional_usd=250, max_total_exposure_usd=250
    )
    account = PaperAccount(ExecutionConfig(), risk)
    shadow = PaperAccount(ExecutionConfig(), risk)
    coordinator = ExecutionCoordinator(Adapter(), account=account, shadow_account=shadow)

    # A probe that fully consumes the shadow budget...
    probe = await coordinator.execute(signal(), is_shadow=True, execution_intent_id="shadow:1")
    assert probe is not None and probe.is_hedged

    # ...leaves the trading budget untouched, so a real signal still fits.
    real = await coordinator.execute(signal(), execution_intent_id="signal:1")
    assert real is not None and real.is_hedged
    assert account.gross_exposure_usd > 0
    assert shadow.gross_exposure_usd == 0, "a probe is released, never settled"


async def test_a_guard_is_checked_inside_the_same_atomic_section_as_every_limit() -> None:
    """The risk engine folds its kill-switch check into ``reserve`` via ``guard``.

    Checking it a moment earlier, outside the account's lock, would leave a
    window between the check and the reservation for the switch to flip in -
    exactly the kill-switch race the risk engine has to close.
    """
    account = PaperAccount(ExecutionConfig(), RiskConfig())
    halted = False

    def guard() -> AccountRejection | None:
        return AccountRejection(RejectionCode.RISK_PAUSED, "halted") if halted else None

    first = await account.reserve(signal(), "before-halt", guard=guard)
    assert not isinstance(first, AccountRejection)

    halted = True
    second = await account.reserve(signal(), "after-halt", guard=guard)
    assert isinstance(second, AccountRejection)
    assert second.code is RejectionCode.RISK_PAUSED

    # The guard runs *before* the idempotency shortcuts, so re-offering an
    # intent that already holds a reservation cannot walk past a halt that
    # arrived in between.
    retried = await account.reserve(signal(), "before-halt", guard=guard)
    assert isinstance(retried, AccountRejection)
    assert retried.code is RejectionCode.RISK_PAUSED

    # Unblocking again lets a fresh reservation through - the guard gates
    # admission, it does not corrupt the account's own bookkeeping.
    halted = False
    third = await account.reserve(signal(), "after-rearm", guard=guard)
    assert not isinstance(third, AccountRejection)


async def test_an_aborted_intent_rechecks_limits_when_it_is_retried() -> None:
    """A pre-submission release is not a completed execution."""
    risk = RiskConfig(
        max_order_notional_usd=200,
        max_position_notional_usd=250,
        max_total_exposure_usd=250,
    )
    account = PaperAccount(ExecutionConfig(), risk)
    abandoned = await account.reserve(signal(), "abandoned")
    assert not isinstance(abandoned, AccountRejection)
    await account.release(abandoned)

    competing = await account.reserve(signal(), "competing")
    assert not isinstance(competing, AccountRejection)
    retried = await account.reserve(signal(), "abandoned")

    assert isinstance(retried, AccountRejection)
    assert retried.code is RejectionCode.EXPOSURE_LIMIT


async def test_retrying_a_completed_intent_does_not_double_count_exposure() -> None:
    adapter = Adapter()
    account = PaperAccount(ExecutionConfig(), RiskConfig())
    coordinator = ExecutionCoordinator(adapter, account=account)

    first = await coordinator.execute(signal(), execution_intent_id="same-intent")
    gross_after_first = account.gross_exposure_usd
    second = await coordinator.execute(signal(), execution_intent_id="same-intent")

    assert first is not None and second is not None
    assert gross_after_first > 0
    assert account.gross_exposure_usd == gross_after_first
