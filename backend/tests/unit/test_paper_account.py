"""Paper execution cannot assume infinite balances, margin or exposure."""

from __future__ import annotations

from tests.unit.test_execution_coordinator import Adapter, opportunity, signal
from trading_bot.core.config import ExecutionConfig, RiskConfig
from trading_bot.execution.account import PaperAccount
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
    coordinator = ExecutionCoordinator(adapter, account=account)
    before_cash = account.cash_usd

    first = await coordinator.execute(signal(), is_shadow=True, execution_intent_id="shadow:1")
    second = await coordinator.execute(signal(), is_shadow=True, execution_intent_id="shadow:2")

    assert first is not None and second is not None
    assert first.is_hedged and second.is_hedged
    assert account.cash_usd == before_cash
    assert account.gross_exposure_usd == 0


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
