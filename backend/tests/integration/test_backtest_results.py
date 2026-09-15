"""What a finished run says: its starting state, its curve, its verdict and its money."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from decimal import Decimal
from itertools import pairwise
from typing import Any

import pytest

from tests.integration.backtest_support import (
    PAIR,
    PERP,
    SPOT,
    START,
    DatasetBuilder,
    backtest_settings,
    cleanup,
    normalized_results,
    seed,
    with_factory,
)
from tests.integration.test_backtest_execution import basis_path, fetch
from trading_bot.backtest.report import build_report
from trading_bot.backtest.report_render import render
from trading_bot.backtest.service import run_backtest
from trading_bot.db.models import Fill, Order, PnlSnapshot, PortfolioSnapshot, Position, RiskEvent
from trading_bot.db.models.enums import BacktestRunStatus, PositionStatus, RiskEventType

pytestmark = pytest.mark.requires_postgres

NO_EXITS = {"exits": {"enabled": False}}


@pytest.fixture(autouse=True)
def _clean(postgres_url: str) -> Iterator[None]:
    cleanup()
    yield
    cleanup()


def run(settings: Any, *, seconds: float, start: Any = START) -> Any:
    return run_backtest(settings, start=start, end=start + timedelta(seconds=seconds), refs=PAIR)


def report_for(outcome: Any) -> Any:
    return with_factory(lambda factory: build_report(factory, outcome.run.run_uid))


class TestInitialization:
    def test_a_start_between_samples_trades_on_the_state_already_in_force(self) -> None:
        dataset = DatasetBuilder()
        # Sampled every 5 s; the run starts 1 s after one sample, 4 s before
        # the next. Funding was last polled 20 s before the start.
        for step in range(-1, 12):
            at = START + timedelta(seconds=5 * step - 1)
            dataset.market_pair(at, spot_mid=Decimal(100_000), basis_bps=Decimal(20))
        dataset.observe_funding(PERP, START - timedelta(seconds=20), mark="100200")
        dataset.observe_funding(PERP, START + timedelta(seconds=10), mark="100200")
        seed(dataset)
        outcome = run(backtest_settings(portfolio=NO_EXITS), seconds=3)
        assert outcome.progress.initialization_events == 5
        entries = [o for o in fetch(Order, Order.backtest_run_id == outcome.run.id)]
        assert entries, "without initialization nothing is known until START + 4 s"
        assert {order.submitted_at for order in entries} == {START}
        fills = fetch(Fill, Fill.backtest_run_id == outcome.run.id)
        # Priced on the book received 1 s before the start: never on anything
        # the process had not yet received.
        assert {fill.book_local_timestamp for fill in fills} == {START - timedelta(seconds=1)}
        assert outcome.completeness["dataset"]["complete"]

    def test_funding_polled_before_the_start_attributes_a_settlement_soon_after_it(self) -> None:
        settles = START + timedelta(seconds=60)  # 08:00:00, an eight-hour boundary
        dataset = basis_path([(40, 20), (60, 20), (40, 0)])
        dataset.funding = []
        dataset.observe_funding(
            PERP,
            START - timedelta(seconds=20),
            rate="0.0001",
            mark="100200",
            next_funding_time=settles,
        )
        dataset.observe_funding(
            PERP,
            settles + timedelta(seconds=30),
            rate="0.0002",
            mark="100200",
            next_funding_time=settles + timedelta(hours=8),
        )
        seed(dataset)
        outcome = run(backtest_settings(), seconds=130)
        perp = next(
            p
            for p in fetch(Position, Position.backtest_run_id == outcome.run.id)
            if p.side.value == "SELL"
        )
        assert perp.status is PositionStatus.CLOSED
        # Short perp, positive rate: receives quantity x mark x rate, from the
        # observation received 80 s before the settlement - before the start.
        assert perp.funding_pnl_usd == (perp.quantity * Decimal(100_200) * Decimal("0.0001"))

    def test_repeated_runs_with_initial_state_are_identical(self) -> None:
        dataset = basis_path([(60, 20), (60, 0)], start=START - timedelta(seconds=10))
        seed(dataset)
        first = run(backtest_settings(), seconds=90)
        second = run(backtest_settings(), seconds=90)
        assert first.fingerprint == second.fingerprint
        assert first.progress.initialization_events == second.progress.initialization_events > 0
        assert normalized_results(first.run.id) == normalized_results(second.run.id)


class TestEquityBaseline:
    def test_a_trade_on_the_first_evaluation_is_inside_the_curve_not_hidden_in_its_baseline(
        self,
    ) -> None:
        start = START + timedelta(seconds=30)  # between two snapshot grid points
        # Enter immediately on a profitable basis, then let it widen before
        # the first post-entry grid snapshot.  With the test suite's zero fee
        # schedule, a permanently favourable basis would never draw down.
        seed(basis_path([(40, 20), (260, 60)]))
        outcome = run(backtest_settings(portfolio=NO_EXITS), seconds=150, start=start)
        orders = fetch(Order, Order.backtest_run_id == outcome.run.id)
        entries = [order for order in orders if not order.execution_intent_id.startswith("close:")]
        assert {order.submitted_at for order in entries} == {start}, "traded on the first tick"
        snapshots = sorted(
            fetch(PortfolioSnapshot, PortfolioSnapshot.backtest_run_id == outcome.run.id),
            key=lambda row: row.captured_at,
        )
        baseline = snapshots[0]
        assert baseline.captured_at == START, "stamped on its grid floor"
        assert baseline.equity_usd == Decimal(100_000) and baseline.open_positions == 0
        stamps = [row.captured_at for row in snapshots]
        assert len(set(stamps)) == len(stamps)
        grid = stamps[:-1]
        assert all(b - a == timedelta(minutes=1) for a, b in pairwise(grid))
        assert stamps[-1] == start + timedelta(seconds=150), "terminal valuation at the end"
        # The adverse revaluation is not hidden in the pre-trade baseline.
        worst = min(row.equity_usd for row in snapshots if row.equity_usd is not None)
        assert worst < Decimal(100_000)
        report = report_for(outcome)
        assert report.max_drawdown_usd == Decimal(100_000) - worst
        # The incremental P&L rows agree with the report computed from history.
        (final_all,) = [
            row
            for row in fetch(PnlSnapshot, PnlSnapshot.backtest_run_id == outcome.run.id)
            if row.window == "all"
            and row.scope_key == "portfolio"
            and row.captured_at == stamps[-1]
        ]
        assert final_all.max_drawdown_usd == report.max_drawdown_usd


class TestCompleteness:
    def test_no_trades_on_complete_data_is_a_rankable_result(self) -> None:
        seed(basis_path([(40, 0)]))
        outcome = run(backtest_settings(), seconds=30)
        assert outcome.status is BacktestRunStatus.COMPLETED
        assert outcome.completeness["performance_rankable"]
        report = report_for(outcome)
        assert report.trade_count == 0 and report.caveat is None
        assert "rankable=yes" in render(report)

    def test_no_trades_because_depth_is_missing_is_never_rankable(self) -> None:
        dataset = basis_path([(40, 20)])
        dataset.books = [row for row in dataset.books if row["ref"] != PERP]
        seed(dataset)
        outcome = run(backtest_settings(), seconds=30)
        assert fetch(Order, Order.backtest_run_id == outcome.run.id) == []
        assert outcome.status is BacktestRunStatus.INCOMPLETE
        assert outcome.completeness["dataset"]["missing"] == [f"{PERP}:BOOK"]
        report = report_for(outcome)
        assert not report.performance_rankable
        assert report.as_dict()["caveat"] == "INCOMPLETE dataset: not a rankable result"
        assert "[INCOMPLETE dataset: not a rankable result]" in render(report)

    def test_missing_funding_is_named(self) -> None:
        dataset = basis_path([(40, 20)])
        dataset.funding = []
        seed(dataset)
        outcome = run(backtest_settings(), seconds=30)
        assert outcome.status is BacktestRunStatus.INCOMPLETE
        assert outcome.completeness["dataset"]["missing"] == [f"{PERP}:FUNDING"]

    def test_an_open_perpetual_remains_measured_when_a_documented_settlement_is_posted(
        self,
    ) -> None:
        seed(basis_path([(120, 20)]))
        quiet = run(backtest_settings(portfolio=NO_EXITS), seconds=50)  # ends 07:59:50
        assert quiet.completeness["accounting"] == {"complete": True, "unmeasured": []}
        crossed = run(backtest_settings(portfolio=NO_EXITS), seconds=70)  # past 08:00:00
        assert crossed.completeness["accounting"] == {"complete": True, "unmeasured": []}
        assert crossed.status is BacktestRunStatus.COMPLETED
        report = report_for(crossed)
        assert report.pnl_complete
        assert report_for(quiet).pnl_complete

    def test_unreproducible_venue_filters_are_declared_fidelity_not_incompleteness(self) -> None:
        seed(basis_path([(40, 0)]))
        outcome = run(backtest_settings(), seconds=30)
        limitations = outcome.completeness["execution_model"]["limitations"]
        assert any(item.startswith("venue_filters_unreproducible") for item in limitations)
        assert any(item.startswith("serialized_scheduling") for item in limitations)
        assert outcome.status is BacktestRunStatus.COMPLETED
        report = report_for(outcome)
        assert report.fidelity_limitations == limitations
        assert "serialized_scheduling" in render(report)


def fees(**extra: Any) -> Any:
    return backtest_settings(
        costs={"spot_taker_fee_bps": 2.0, "perp_taker_fee_bps": 2.0, **extra.pop("costs", {})},
        **extra,
    )


class TestReportAccounting:
    def test_positions_open_at_the_end_are_valued_not_closed_and_their_fees_count(self) -> None:
        seed(basis_path([(40, 20)]))
        outcome = run(fees(portfolio=NO_EXITS), seconds=30)
        run_id = outcome.run.id
        assert {o.intent for o in fetch(Order, Order.backtest_run_id == run_id)} == {"OPEN"}
        assert {p.status for p in fetch(Position, Position.backtest_run_id == run_id)} == {
            PositionStatus.OPEN
        }
        fills = fetch(Fill, Fill.backtest_run_id == run_id)
        report = report_for(outcome)
        assert report.trade_count == 0 and report.completed_trade_fees_usd == 0
        assert report.execution_fees_usd == sum(fill.fee_usd for fill in fills) > 0
        assert report.fees_paid_in_cash_usd == report.execution_fees_usd
        assert report.turnover_usd == sum(fill.price * fill.quantity for fill in fills)
        assert report.open_attempts == 1 and report.realized_pnl_usd == 0
        assert report.exposure_time_pct == pytest.approx(100.0, abs=0.5)
        assert abs(report.equity_reconciliation_difference_usd) < Decimal("0.0001")

    def test_fees_paid_in_bnb_are_totalled_apart_and_the_equity_still_reconciles(self) -> None:
        seed(basis_path([(40, 20)]))
        settings = fees(
            costs={"pay_fees_in_bnb": True},
            execution={"paper_bnb_balance": 10.0, "paper_bnb_price_usd": 500.0},
            portfolio=NO_EXITS,
        )
        outcome = run(settings, seconds=30)
        assert outcome.status is not BacktestRunStatus.FAILED, outcome.failure_reason
        report = report_for(outcome)
        assert report.fees_paid_in_cash_usd == 0
        assert report.fees_paid_in_bnb_usd == report.execution_fees_usd > 0
        assert report.expected_final_equity_usd == report.final_equity_usd
        assert report.final_equity_usd == (
            report.initial_cash_usd
            + report.realized_pnl_all_legs_usd
            + (report.unrealized_pnl_usd or Decimal(0))
            - report.fees_paid_in_bnb_usd
        )
        assert abs(report.equity_reconciliation_difference_usd) < Decimal("0.0001")

    def test_a_closed_trade_separates_its_fees_from_the_total(self) -> None:
        seed(basis_path([(20, 20), (60, 0)]))
        outcome = run(fees(), seconds=70)
        report = report_for(outcome)
        assert report.trade_count == 1
        assert report.completed_trade_fees_usd == report.execution_fees_usd
        assert report.winning_trades + report.losing_trades + report.breakeven_trades == 1
        assert report.average_trade_usd == report.realized_pnl_usd
        assert report.by_strategy["spot_perp_basis"]["trades"] == 1
        assert abs(report.equity_reconciliation_difference_usd) < Decimal("0.0001")
        assert report.realized_pnl_all_legs_usd == report.realized_pnl_usd

    @pytest.mark.parametrize(
        ("spot_size", "perp_size", "naked_quantity"),
        [("0.004", "1", Decimal("0.005")), ("0.004", "0.006", Decimal("0.002"))],
    )
    def test_one_leg_and_unequal_partial_fills_count_their_fees_and_naked_exposure(
        self, spot_size: str, perp_size: str, naked_quantity: Decimal
    ) -> None:
        dataset = basis_path([(1, 20)])
        thin = START + timedelta(milliseconds=50)
        dataset.book(SPOT, thin, "99999.5", "100000.5", levels=1, size=spot_size)
        dataset.book(PERP, thin, "100199.5", "100200.5", levels=1, size=perp_size)
        rest = basis_path([(1, 20), (20, 20)])
        later = START + timedelta(seconds=1)
        dataset.quotes += [r for r in rest.quotes if r["local_timestamp"] >= later]
        dataset.books += [r for r in rest.books if r["local_timestamp"] >= later]
        seed(dataset)
        outcome = run(fees(backtest={"exits_enabled": False}), seconds=10)
        fills = fetch(Fill, Fill.backtest_run_id == outcome.run.id)
        orders = fetch(Order, Order.backtest_run_id == outcome.run.id)
        report = report_for(outcome)
        assert report.attempts == {"unhedged": 1}
        assert report.trade_count == 0
        assert report.execution_fees_usd == sum(fill.fee_usd for fill in fills)
        heavier = max(orders, key=lambda order: order.filled_quantity)
        assert report.worst_unhedged_notional_usd == naked_quantity * heavier.average_fill_price


class TestExecutionRealism:
    def test_legs_leave_together_and_cross_leg_skew_is_recorded_without_halting(self) -> None:
        dataset = basis_path([(1, 20)])
        for offset in (100, 400):
            at = START + timedelta(milliseconds=offset)
            dataset.book(SPOT, at, "99999.5", "100000.5")
            dataset.book(PERP, at, "100199.5", "100200.5")
        rest = basis_path([(1, 20), (10, 20)])
        later = START + timedelta(seconds=1)
        dataset.quotes += [r for r in rest.quotes if r["local_timestamp"] >= later]
        dataset.books += [r for r in rest.books if r["local_timestamp"] >= later]
        seed(dataset)
        settings = backtest_settings(
            execution={"latency_ms_by_market_type": {"SPOT": 100, "PERPETUAL": 400}},
            portfolio=NO_EXITS,
        )
        outcome = run(settings, seconds=5)
        orders = fetch(Order, Order.backtest_run_id == outcome.run.id)
        assert {order.submitted_at for order in orders} == {START}, "sent concurrently"
        fills = fetch(Fill, Fill.backtest_run_id == outcome.run.id)
        assert sorted(fill.book_local_timestamp for fill in fills) == [
            START + timedelta(milliseconds=100),
            START + timedelta(milliseconds=400),
        ]
        events = fetch(RiskEvent, RiskEvent.backtest_run_id == outcome.run.id)
        skew = [e for e in events if e.limit_name == "max_leg_skew_ms"]
        assert skew and skew[0].observed_value == Decimal(300)
        assert not [e for e in events if e.event_type is RiskEventType.KILL_SWITCH]
