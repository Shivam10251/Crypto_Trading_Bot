"""Run isolation and enum enforcement, as PostgreSQL itself enforces them."""

from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from tests.integration.factories import NOW, make_market, make_opportunity, make_order
from trading_bot.db.models import (
    BacktestRun,
    ExecutionMode,
    Order,
    PnlSnapshot,
    PortfolioSnapshot,
    Position,
    RiskEvent,
)
from trading_bot.db.models.enums import (
    BacktestRunStatus,
    PositionStatus,
    RiskDecision,
    RiskEventType,
    Side,
    ValuationStatus,
)

pytestmark = pytest.mark.requires_postgres


async def make_run(db: AsyncSession) -> BacktestRun:
    run = BacktestRun(
        run_uid=uuid.uuid4(),
        status=BacktestRunStatus.PENDING,
        dataset_source="postgres",
        requested_start=NOW,
        requested_end=NOW + timedelta(hours=1),
        markets=[],
        config_snapshot={},
        config_hash="0" * 64,
    )
    db.add(run)
    await db.flush()
    return run


async def violates(db: AsyncSession, constraint: str, work: Any) -> None:
    savepoint = await db.begin_nested()
    with pytest.raises(IntegrityError, match=constraint):
        await work()
    await savepoint.rollback()


class TestEnumsAreEnforcedByTheDatabase:
    async def test_an_unknown_mode_is_refused_by_a_raw_writer(self, db: AsyncSession) -> None:
        market = make_market()
        db.add(market)
        await db.flush()

        async def insert() -> None:
            await db.execute(
                text(
                    "INSERT INTO orders (market_id, mode, client_order_id, side, order_type, "
                    "quantity, filled_quantity, status) VALUES "
                    "(:market, 'PAPR', 'raw', 'BUY', 'MARKET', 1, 0, 'PENDING')"
                ),
                {"market": market.id},
            )

        await violates(db, "ck_orders_execution_mode", insert)

    async def test_an_unknown_status_is_refused_on_every_enum_column(
        self, db: AsyncSession
    ) -> None:
        async def insert() -> None:
            await db.execute(
                text(
                    "INSERT INTO system_events (occurred_at, event_type, severity, component, "
                    "message) VALUES (now(), 'STARTUP', 'LOUD', 'x', 'y')"
                )
            )

        await violates(db, "ck_system_events_severity", insert)


class TestModeAndRunAgree:
    async def test_a_backtest_row_needs_its_run(self, db: AsyncSession) -> None:
        market = make_market()
        db.add(market)
        await db.flush()

        async def insert() -> None:
            db.add(make_order(market, mode=ExecutionMode.BACKTEST, client_order_id="orphan"))
            await db.flush()

        await violates(db, "ck_orders_backtest_mode_has_run", insert)

    async def test_a_paper_row_cannot_belong_to_a_run(self, db: AsyncSession) -> None:
        market = make_market()
        db.add(market)
        run = await make_run(db)

        async def insert() -> None:
            db.add(make_order(market, backtest_run_id=run.id, client_order_id="stray"))
            await db.flush()

        await violates(db, "ck_orders_backtest_mode_has_run", insert)


class TestRunKeyedUniqueness:
    async def test_two_runs_may_repeat_every_deterministic_identity(self, db: AsyncSession) -> None:
        market = make_market()
        db.add(market)
        first, second = await make_run(db), await make_run(db)
        uid = uuid.uuid4()
        for run in (first, second):
            scope = {"mode": ExecutionMode.BACKTEST, "backtest_run_id": run.id}
            db.add(
                make_order(
                    market,
                    client_order_id="a1-0",
                    execution_intent_id="signal:x",
                    signal_leg=0,
                    **scope,
                )
            )
            db.add(make_opportunity(market, uid=uid, **scope))
            db.add(
                PortfolioSnapshot(
                    captured_at=NOW,
                    cash_usd=Decimal(1),
                    position_value_usd=Decimal(0),
                    equity_usd=Decimal(1),
                    **scope,
                )
            )
            db.add(PnlSnapshot(captured_at=NOW, window="all", scope_key="portfolio", **scope))
            db.add(
                RiskEvent(
                    occurred_at=NOW,
                    event_type=RiskEventType.PRE_TRADE_CHECK,
                    decision=RiskDecision.APPROVED,
                    intent_id="signal:x",
                    reason="ok",
                    **scope,
                )
            )
            db.add(
                Position(
                    market=market,
                    attempt_id="a1",
                    strategy="s",
                    side=Side.BUY,
                    status=PositionStatus.OPEN,
                    quantity=Decimal(1),
                    entry_price=Decimal(1),
                    entry_notional_usd=Decimal(1),
                    opened_at=NOW,
                    **scope,
                )
            )
        await db.flush()
        assert (
            await db.scalar(select(func.count(Order.id)).where(Order.client_order_id == "a1-0"))
            == 2
        )

    async def test_one_run_still_refuses_a_duplicate_order(self, db: AsyncSession) -> None:
        market = make_market()
        db.add(market)
        run = await make_run(db)
        scope = {"mode": ExecutionMode.BACKTEST, "backtest_run_id": run.id}
        db.add(make_order(market, client_order_id="dup", **scope))
        await db.flush()

        async def duplicate() -> None:
            db.add(make_order(market, client_order_id="dup", **scope))
            await db.flush()

        await violates(db, "mode_run_client_order_id", duplicate)

    async def test_outside_a_run_duplicates_still_collide(self, db: AsyncSession) -> None:
        """NULLS NOT DISTINCT: the paper service's protection did not weaken."""
        market = make_market()
        db.add(market)
        db.add(make_order(market, client_order_id="paper-dup"))
        db.add(
            PortfolioSnapshot(
                captured_at=NOW,
                mode=ExecutionMode.PAPER,
                cash_usd=Decimal(1),
                position_value_usd=Decimal(0),
                equity_usd=Decimal(1),
                valuation_status=ValuationStatus.COMPLETE,
            )
        )
        await db.flush()

        async def order() -> None:
            db.add(make_order(market, client_order_id="paper-dup"))
            await db.flush()

        async def snapshot() -> None:
            db.add(
                PortfolioSnapshot(
                    captured_at=NOW,
                    mode=ExecutionMode.PAPER,
                    cash_usd=Decimal(2),
                    position_value_usd=Decimal(0),
                    equity_usd=Decimal(2),
                )
            )
            await db.flush()

        await violates(db, "mode_run_client_order_id", order)
        await violates(db, "mode_run_captured_at", snapshot)

    async def test_legacy_orders_without_intent_keys_still_coexist(self, db: AsyncSession) -> None:
        """Every pre-remediation order has NULL intent keys; they must not collide."""
        market = make_market()
        db.add(market)
        db.add(make_order(market, client_order_id="legacy-1"))
        db.add(make_order(market, client_order_id="legacy-2"))
        await db.flush()
        db.add(make_order(market, client_order_id="new-1", execution_intent_id="i", signal_leg=0))
        await db.flush()

        async def same_intent() -> None:
            db.add(
                make_order(market, client_order_id="new-2", execution_intent_id="i", signal_leg=0)
            )
            await db.flush()

        await violates(db, "ux_orders_live_intent_leg", same_intent)

    async def test_deleting_a_run_deletes_what_it_produced_and_nothing_else(
        self, db: AsyncSession
    ) -> None:
        market = make_market()
        db.add(market)
        run = await make_run(db)
        db.add(make_order(market, client_order_id="kept"))
        db.add(
            make_order(
                market, client_order_id="gone", mode=ExecutionMode.BACKTEST, backtest_run_id=run.id
            )
        )
        await db.flush()
        await db.execute(delete(BacktestRun).where(BacktestRun.id == run.id))
        remaining = (await db.execute(select(Order.client_order_id))).scalars().all()
        assert "kept" in remaining and "gone" not in remaining


class TestRunLifecycleConstraints:
    async def test_a_failed_run_must_say_why(self, db: AsyncSession) -> None:
        run = await make_run(db)

        async def fail() -> None:
            run.status = BacktestRunStatus.FAILED
            run.started_at = NOW
            run.completed_at = NOW
            await db.flush()

        await violates(db, "ck_backtest_runs_failed_has_reason", fail)

    async def test_a_terminal_run_says_when_it_ended(self, db: AsyncSession) -> None:
        run = await make_run(db)

        async def complete() -> None:
            run.status = BacktestRunStatus.COMPLETED
            run.started_at = NOW
            await db.flush()

        await violates(db, "ck_backtest_runs_terminal_has_completed_at", complete)
