"""Execution, risk, exits and accounting under replay - the real behaviour, not a kinder one."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tests.integration.backtest_support import (
    PAIR,
    PERP,
    SPOT,
    START,
    DatasetBuilder,
    backtest_settings,
    cleanup,
    run_async,
    seed,
    with_factory,
)
from trading_bot.backtest.report import build_report
from trading_bot.backtest.service import run_backtest
from trading_bot.db.models import (
    BacktestFundingPayment,
    Fill,
    Order,
    PortfolioSnapshot,
    Position,
    RiskEvent,
)
from trading_bot.db.models.enums import (
    BacktestRunStatus,
    OrderStatus,
    PositionStatus,
    RiskDecision,
    RiskEventType,
)

pytestmark = pytest.mark.requires_postgres


@pytest.fixture(autouse=True)
def _clean(postgres_url: str) -> Iterator[None]:
    cleanup()
    yield
    cleanup()


def fetch(model: Any, *conditions: Any) -> list[Any]:
    async def read(session: AsyncSession) -> list[Any]:
        return list((await session.execute(select(model).where(*conditions))).scalars())

    return run_async(read)


def basis_path(
    phases: list[tuple[int, int]],
    *,
    start: datetime = START,
    funding_every: int = 30,
) -> DatasetBuilder:
    """``(seconds, basis_bps)`` phases, one step a second, funding every 30 s."""
    dataset = DatasetBuilder()
    second = 0
    for length, basis in phases:
        for _ in range(length):
            at = start + timedelta(seconds=second)
            dataset.market_pair(at, spot_mid=Decimal(100_000), basis_bps=Decimal(basis))
            if second % funding_every == 0:
                dataset.observe_funding(PERP, at, mark="100100")
            second += 1
    return dataset


def replay(settings: Any, *, seconds: int, start: datetime = START) -> Any:
    return run_backtest(settings, start=start, end=start + timedelta(seconds=seconds), refs=PAIR)


class TestDepthAndLegRisk:
    def test_truncated_depth_partially_fills_one_leg_pauses_and_the_residual_is_closed(
        self,
    ) -> None:
        dataset = basis_path([(1, 20)])
        thin = START + timedelta(milliseconds=50)
        # At arrival the spot asks hold 0.004 of the 0.009 ordered, and the
        # recording says nothing about depth beyond it.
        dataset.book(SPOT, thin, "99999.5", "100000.5", levels=1, size="0.004")
        dataset.book(PERP, thin, "100199.5", "100200.5")
        later = basis_path([(1, 20), (4, 20), (5, 0), (10, 20)], start=START)
        dataset.quotes += [
            row for row in later.quotes if row["local_timestamp"] >= START + timedelta(seconds=1)
        ]
        dataset.books += [
            row for row in later.books if row["local_timestamp"] >= START + timedelta(seconds=1)
        ]
        dataset.funding += later.funding[1:]
        seed(dataset)
        outcome = replay(backtest_settings(), seconds=18)
        run_id = outcome.run.id

        orders = fetch(Order, Order.backtest_run_id == run_id)
        spot_entry = next(o for o in orders if o.intent == "OPEN" and o.side.value == "BUY")
        assert spot_entry.status is OrderStatus.PARTIALLY_FILLED
        assert spot_entry.filled_quantity == Decimal("0.004")
        assert (spot_entry.evidence or {})["rejection_code"] == "DEPTH_TRUNCATED"

        events = fetch(RiskEvent, RiskEvent.backtest_run_id == run_id)
        abnormal = [e for e in events if e.event_type is RiskEventType.ABNORMAL_EXECUTION]
        assert abnormal and abnormal[0].decision is RiskDecision.PAUSED
        halts = [
            e
            for e in events
            if e.event_type is RiskEventType.KILL_SWITCH and e.decision is RiskDecision.PAUSED
        ]
        # The trigger itself, and the later episode it refused.
        assert len(halts) >= 2

        positions = fetch(Position, Position.backtest_run_id == run_id)
        assert {p.status for p in positions} == {PositionStatus.CLOSED}
        assert {p.exit_reason for p in positions} == {"UNPAIRED_RESIDUAL"}
        closes = [o for o in orders if o.intent == "CLOSE"]
        assert sorted(o.filled_quantity for o in closes) == [Decimal("0.004"), Decimal("0.009")]

        report = with_factory(lambda factory: build_report(factory, outcome.run.run_uid))
        assert report.attempts == {"unhedged": 1}


class TestFeesSlippageAndOrderTypes:
    def test_fees_are_charged_once_and_slippage_is_attribution_only(self) -> None:
        dataset = basis_path([(1, 60)])
        moved = START + timedelta(milliseconds=50)
        dataset.book(SPOT, moved, "100000.5", "100001.5")  # 1 USD worse to buy
        dataset.book(PERP, moved, "100599.5", "100600.5")
        rest = basis_path([(1, 60), (9, 60), (20, 0)])
        dataset.quotes += [
            r for r in rest.quotes if r["local_timestamp"] >= START + timedelta(seconds=1)
        ]
        dataset.books += [
            r for r in rest.books if r["local_timestamp"] >= START + timedelta(seconds=1)
        ]
        dataset.funding += rest.funding[1:]
        seed(dataset)
        settings = backtest_settings(
            costs={"spot_taker_fee_bps": 10.0, "perp_taker_fee_bps": 5.0},
            strategy={"spot_perp_basis": {"min_net_edge_bps": 1.0}},
        )
        outcome = replay(settings, seconds=25)
        run_id = outcome.run.id
        fills = fetch(Fill, Fill.backtest_run_id == run_id)
        orders = {o.id: o for o in fetch(Order, Order.backtest_run_id == run_id)}
        assert fills and all(fill.is_maker is False for fill in fills)
        for fill in fills:
            rate = (
                Decimal(10)
                if orders[fill.order_id].market_id == min(o.market_id for o in orders.values())
                else Decimal(5)
            )
            assert fill.fee_rate_bps == rate
            assert fill.fee_usd == (fill.price * fill.quantity * rate / 10_000).quantize(
                Decimal("0.00000001")
            )
        spot_buy = next(
            f
            for f in fills
            if orders[f.order_id].intent == "OPEN" and orders[f.order_id].side.value == "BUY"
        )
        assert spot_buy.price == Decimal("100001.5")
        assert spot_buy.slippage_bps is not None and spot_buy.slippage_bps > 0

        positions = fetch(Position, Position.backtest_run_id == run_id)
        assert {p.status for p in positions} == {PositionStatus.CLOSED}
        for position in positions:
            # Price P&L minus fees, and nothing for slippage: it is already
            # inside the fill prices.
            assert position.realized_pnl_usd == position.price_pnl_usd - position.fees_usd + (
                position.funding_pnl_usd or Decimal(0)
            )
        spot = next(p for p in positions if p.side.value == "BUY")
        assert spot.slippage_usd == Decimal("0.009")

    def test_an_ioc_limit_whose_price_the_market_left_expires_unfilled(self) -> None:
        dataset = basis_path([(1, 20)])
        moved = START + timedelta(milliseconds=50)
        dataset.book(SPOT, moved, "100050", "100051")
        dataset.book(PERP, moved, "100150", "100151")
        seed(dataset)
        outcome = replay(backtest_settings(execution={"entry_order_type": "limit"}), seconds=1)
        orders = fetch(Order, Order.backtest_run_id == outcome.run.id)
        assert len(orders) == 2
        # Both limits sat at the decision's prices; the book moved away from
        # both before arrival. Nothing crossed, and nothing rested to be
        # "filled as a maker" later.
        assert {o.status for o in orders} == {OrderStatus.EXPIRED}
        assert all(o.filled_quantity == 0 for o in orders)
        assert fetch(Fill, Fill.backtest_run_id == outcome.run.id) == []
        assert fetch(Position, Position.backtest_run_id == outcome.run.id) == []
        report = with_factory(lambda factory: build_report(factory, outcome.run.run_uid))
        assert report.attempts == {"nothing_filled": 1}
        assert report.maker_fills == 0


class TestRiskAndExits:
    def test_the_gross_exposure_limit_refuses_the_pair(self) -> None:
        seed(basis_path([(5, 20)]))
        settings = backtest_settings(
            risk={
                "max_order_notional_usd": 1_000.0,
                "max_position_notional_usd": 1_000.0,
                "max_total_exposure_usd": 1_500.0,
            }
        )
        outcome = replay(settings, seconds=3)
        events = fetch(RiskEvent, RiskEvent.backtest_run_id == outcome.run.id)
        assert [e.event_type for e in events] == [RiskEventType.EXPOSURE_LIMIT_EXCEEDED]
        assert fetch(Order, Order.backtest_run_id == outcome.run.id) == []

    def test_a_position_that_never_converges_is_closed_at_its_holding_limit(self) -> None:
        seed(basis_path([(60, 20)]))
        settings = backtest_settings(portfolio={"exits": {"max_holding_minutes": 0.5}})
        outcome = replay(settings, seconds=45)
        positions = fetch(Position, Position.backtest_run_id == outcome.run.id)
        assert {p.exit_reason for p in positions} == {"MAX_HOLDING_PERIOD"}
        # Opened at the +100 ms fill; the first sweep past 30 s is at 31 s, and
        # its fills arrive 100 ms later - all in virtual time.
        assert {p.opened_at for p in positions} == {START + timedelta(milliseconds=100)}
        assert {p.closed_at for p in positions} == {START + timedelta(seconds=31, milliseconds=100)}

    def test_the_daily_loss_limit_is_a_virtual_utc_day(self) -> None:
        night = datetime(2026, 8, 3, 23, 58, tzinfo=UTC)
        seed(
            basis_path(
                [(10, 20), (10, 80), (10, 0), (10, 20), (90, 0), (15, 20)],
                start=night,
            )
        )
        settings = backtest_settings(
            risk={"max_daily_loss_usd": 1.0, "daily_loss_halt_minutes": 1},
            portfolio={"exits": {"adverse_basis_bps": 25.0}},
        )
        outcome = replay(settings, seconds=145, start=night)
        events = fetch(RiskEvent, RiskEvent.backtest_run_id == outcome.run.id)
        positions = fetch(Position, Position.backtest_run_id == outcome.run.id)
        assert {
            p.exit_reason for p in positions if p.opened_at < night + timedelta(seconds=10)
        } == {"ADVERSE_BASIS"}
        breach = [e for e in events if e.event_type is RiskEventType.DAILY_LOSS_LIMIT]
        assert breach and breach[0].occurred_at == night + timedelta(seconds=30)
        approvals = sorted(
            e.occurred_at
            for e in events
            if e.event_type is RiskEventType.PRE_TRADE_CHECK and e.decision is RiskDecision.APPROVED
        )
        # One entry before the loss, none while halted, and one on the new UTC
        # day, whose realised P&L starts again from nothing.
        assert approvals == [night, datetime(2026, 8, 4, 0, 0, 10, tzinfo=UTC)]


class TestFundingAccounting:
    def test_a_settlement_crossed_with_evidence_is_attributed_and_settles_into_cash(self) -> None:
        seed(basis_path([(90, 20), (60, 0)]))
        outcome = replay(backtest_settings(), seconds=140)
        positions = fetch(Position, Position.backtest_run_id == outcome.run.id)
        perp = next(p for p in positions if p.side.value == "SELL")
        spot = next(p for p in positions if p.side.value == "BUY")
        # Short 0.009 at the 08:00 settlement, observed 30 s before at a mark
        # of 100,100 and a rate of 0.0001: received 0.09009.
        assert perp.closed_at > datetime(2026, 8, 3, 8, 0, tzinfo=UTC)
        assert perp.funding_pnl_usd == Decimal("0.09009")
        assert perp.unmeasured_pnl is None
        (payment,) = fetch(
            BacktestFundingPayment,
            BacktestFundingPayment.backtest_run_id == outcome.run.id,
        )
        assert payment.settled_at == datetime(2026, 8, 3, 8, 0, tzinfo=UTC)
        assert payment.amount_usd == Decimal("0.09009")
        report = with_factory(lambda factory: build_report(factory, outcome.run.run_uid))
        assert report.funding_usd == Decimal("0.09009")
        assert report.pnl_complete
        last = max(
            fetch(PortfolioSnapshot, PortfolioSnapshot.backtest_run_id == outcome.run.id),
            key=lambda row: row.captured_at,
        )
        cash_flows = (spot.exit_notional_usd - spot.entry_notional_usd) + perp.price_pnl_usd
        assert last.cash_usd == Decimal(100_000) + cash_flows + Decimal("0.09009")
        at_settlement = next(
            row
            for row in fetch(PortfolioSnapshot, PortfolioSnapshot.backtest_run_id == outcome.run.id)
            if row.captured_at == datetime(2026, 8, 3, 8, 0, tzinfo=UTC)
        )
        assert at_settlement.open_positions == 2
        assert at_settlement.cash_usd == Decimal(100_000) - spot.entry_notional_usd + Decimal(
            "0.09009"
        )

    def test_without_evidence_close_to_the_settlement_funding_stays_unmeasured(self) -> None:
        seed(basis_path([(90, 20), (60, 0)]))
        settings = backtest_settings(backtest={"funding_settlement_max_age_ms": 10_000})
        outcome = replay(settings, seconds=140)
        perp = next(
            p
            for p in fetch(Position, Position.backtest_run_id == outcome.run.id)
            if p.side.value == "SELL"
        )
        assert perp.funding_pnl_usd is None
        assert perp.unmeasured_pnl == ["funding"]
        report = with_factory(lambda factory: build_report(factory, outcome.run.run_uid))
        assert report.funding_usd is None
        assert not report.pnl_complete
        assert outcome.completeness["accounting"] == {"complete": False, "unmeasured": ["funding"]}
        assert outcome.completeness["dataset"]["complete"]
        assert outcome.status is BacktestRunStatus.INCOMPLETE


class TestDataQuality:
    def test_gaps_duplicates_and_corrupt_rows_are_counted_and_the_run_is_incomplete(self) -> None:
        dataset = basis_path([(5, 20)])
        later = basis_path([(5, 20), (15, 20), (10, 20)])
        # A 15 s hole in every stream after the first five seconds.
        dataset.quotes += [
            r for r in later.quotes if r["local_timestamp"] >= START + timedelta(seconds=20)
        ]
        dataset.books += [
            r for r in later.books if r["local_timestamp"] >= START + timedelta(seconds=20)
        ]
        duplicate = dict(dataset.books[-1])
        duplicate["local_timestamp"] += timedelta(milliseconds=1)
        dataset.books.append(duplicate)
        crossed = dict(dataset.books[0])
        crossed["local_timestamp"] += timedelta(milliseconds=2)
        crossed["sequence"] = 999_999
        crossed["bids"], crossed["asks"] = crossed["asks"], crossed["bids"]
        dataset.books.append(crossed)
        dataset.books.sort(key=lambda row: row["local_timestamp"])
        seed(dataset)
        settings = backtest_settings(backtest={"max_book_carry_ms": 5_000})
        outcome = replay(settings, seconds=30)
        counts = (outcome.dataset_issues or {})["counts"]
        assert counts["gap"] >= 4
        assert counts["duplicate"] == 1
        assert counts["corrupt"] == 1
        assert counts["book_carry_expired"] == 2
        assert outcome.status is BacktestRunStatus.INCOMPLETE
        dataset_verdict = outcome.completeness["dataset"]
        assert not dataset_verdict["complete"]
        assert dataset_verdict["issues"]["book_carry_expired"] == 2
        assert dataset_verdict["issues"]["corrupt"] == 1
        assert dataset_verdict["issues"]["gap"] >= 4
