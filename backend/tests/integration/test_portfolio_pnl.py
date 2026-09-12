"""Snapshots, realised P&L, and the two risk limits that were waiting for it.

What only PostgreSQL can prove here: the snapshot idempotency constraints, the
mode and shadow filters that keep three populations apart, and the fact that
the daily-loss and consecutive-loss gates - built and honestly reported as
deferred in Phase 9 - genuinely fire once a real P&L source exists.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from tests.integration.test_portfolio import (
    NOW,
    PERP,
    SPOT,
    VENUE,
    StubMarks,
    open_attempt,
)
from tests.unit.test_risk_engine import fresh_signal
from trading_bot.api.portfolio_status import portfolio_status
from trading_bot.api.schemas import ComponentStatus
from trading_bot.core.config import (
    ExecutionConfig,
    PortfolioConfig,
    RiskConfig,
    Settings,
)
from trading_bot.db.models import Fill, Order, PnlSnapshot, PortfolioSnapshot, Position
from trading_bot.db.models.enums import (
    ExecutionMode,
    MarketType,
    OrderStatus,
    OrderType,
    PositionStatus,
    RiskDecision,
    RiskEventType,
    Side,
    ValuationStatus,
)
from trading_bot.exchange.models import MarketRef
from trading_bot.execution.account import PaperAccount
from trading_bot.execution.models import OrderIntent
from trading_bot.portfolio.accounting import FUNDING
from trading_bot.portfolio.pnl_source import PortfolioPnlSource, utc_day_start
from trading_bot.portfolio.service import PortfolioService
from trading_bot.portfolio.snapshots import SnapshotWriter, Window, value_book
from trading_bot.portfolio.store import PortfolioStore
from trading_bot.risk.engine import RiskEngine
from trading_bot.risk.kill_switch import KillSwitchState
from trading_bot.risk.store import RiskEventStore

pytestmark = pytest.mark.requires_postgres

SessionFactory = Callable[[], contextlib.AbstractAsyncContextManager[AsyncSession]]
INTERVAL = timedelta(minutes=1)


def session_factory(db: AsyncSession) -> SessionFactory:
    @contextlib.asynccontextmanager
    async def factory() -> AsyncIterator[AsyncSession]:
        yield db

    return factory


@pytest.fixture
async def markets(db: AsyncSession) -> dict[MarketRef, int]:
    from tests.integration.factories import make_market

    spot = make_market("BTCUSDT", MarketType.SPOT)
    perp = make_market("BTCUSDT", MarketType.PERPETUAL)
    db.add_all([spot, perp])
    await db.flush()
    return {SPOT: spot.id, PERP: perp.id}


async def complete_attempt(
    db: AsyncSession,
    markets: dict[MarketRef, int],
    *,
    attempt_id: str,
    buy_exit: str,
    sell_exit: str,
    closed_at: datetime,
    fee: str = "0",
    mode: ExecutionMode = ExecutionMode.PAPER,
    is_shadow: bool = False,
) -> list[int]:
    """An attempt opened at 100000/100100 and closed at the given prices."""
    ids = await open_attempt(
        db, markets, attempt_id=attempt_id, mode=mode, is_shadow=is_shadow, fee=fee
    )
    for index, (ref, side, price) in enumerate(
        ((SPOT, Side.SELL, buy_exit), (PERP, Side.BUY, sell_exit))
    ):
        order = Order(
            market_id=markets[ref],
            mode=mode,
            is_shadow=is_shadow,
            attempt_id=attempt_id,
            execution_intent_id=f"close:{attempt_id}:0",
            signal_leg=index,
            intent=OrderIntent.CLOSE.value,
            strategy="spot_perp_basis",
            client_order_id=f"close:{attempt_id}:0-{index}",
            side=side,
            order_type=OrderType.MARKET,
            quantity=Decimal(1),
            filled_quantity=Decimal(1),
            average_fill_price=Decimal(price),
            status=OrderStatus.FILLED,
            submitted_at=closed_at,
        )
        db.add(order)
        await db.flush()
        db.add(
            Fill(
                order_id=order.id,
                position_id=ids[ref],
                mode=mode,
                price=Decimal(price),
                quantity=Decimal(1),
                fee_usd=Decimal(fee),
                filled_at=closed_at,
                fill_index=0,
            )
        )
    await db.flush()
    store = PortfolioStore(session_factory(db), mode=mode)
    await store.reconcile(list(ids.values()), now=closed_at)
    await db.flush()
    return list(ids.values())


def writer(db: AsyncSession, *, cash: str = "100000") -> SnapshotWriter:
    return SnapshotWriter(
        PortfolioStore(session_factory(db), mode=ExecutionMode.PAPER),
        session_factory(db),
        initial_cash_usd=Decimal(cash),
        fees_paid_in_cash=True,
        interval=INTERVAL,
        min_return_observations=3,
        risk_free_rate_annual_pct=0.0,
    )


class TestPairedResults:
    async def test_a_converged_basis_is_one_winning_trade_not_two_legs(
        self, db: AsyncSession, markets: dict[MarketRef, int]
    ) -> None:
        """Spot +50, perp +50: one trade worth +100, not a win and a loss."""
        await complete_attempt(
            db, markets, attempt_id="t1", buy_exit="100050", sell_exit="100050", closed_at=NOW
        )
        store = PortfolioStore(session_factory(db), mode=ExecutionMode.PAPER)

        attempts = await store.attempts_closed_between(VENUE, None, NOW + timedelta(minutes=1))

        assert len(attempts) == 1
        trade = attempts[0].paired()
        assert trade.is_complete
        assert trade.realized_pnl_usd == Decimal(100)

    async def test_a_hedged_pair_that_moved_together_nets_to_nothing(
        self, db: AsyncSession, markets: dict[MarketRef, int]
    ) -> None:
        """Both legs down 100: the spot leg loses what the perp leg makes."""
        await complete_attempt(
            db, markets, attempt_id="t1", buy_exit="99900", sell_exit="100000", closed_at=NOW
        )
        store = PortfolioStore(session_factory(db), mode=ExecutionMode.PAPER)

        trade = (await store.attempts_closed_between(VENUE, None, NOW + INTERVAL))[0].paired()

        assert trade.realized_pnl_usd == Decimal(0)

    async def test_funding_is_named_as_unmeasured_on_the_stored_row(
        self, db: AsyncSession, markets: dict[MarketRef, int]
    ) -> None:
        ids = await complete_attempt(
            db, markets, attempt_id="t1", buy_exit="100050", sell_exit="100050", closed_at=NOW
        )

        spot = await db.get(Position, ids[0])
        perpetual = await db.get(Position, ids[1])

        assert spot is not None and perpetual is not None
        assert spot.funding_pnl_usd == Decimal(0)  # funding does not apply to spot
        assert spot.unmeasured_pnl is None
        assert perpetual.funding_pnl_usd is None
        assert perpetual.unmeasured_pnl == [FUNDING]


class TestModeAndShadowSeparation:
    async def test_live_results_never_enter_a_paper_total(
        self, db: AsyncSession, markets: dict[MarketRef, int]
    ) -> None:
        await complete_attempt(
            db,
            markets,
            attempt_id="live",
            buy_exit="200000",
            sell_exit="100050",
            closed_at=NOW,
            mode=ExecutionMode.LIVE,
        )
        store = PortfolioStore(session_factory(db), mode=ExecutionMode.PAPER)

        assert await store.attempts_closed_between(VENUE, None, NOW + INTERVAL) == []

    async def test_shadow_probes_never_enter_an_actionable_total(
        self, db: AsyncSession, markets: dict[MarketRef, int]
    ) -> None:
        """A probe measures the venue, not what the strategy would have earned."""
        await complete_attempt(
            db,
            markets,
            attempt_id="probe",
            buy_exit="200000",
            sell_exit="100050",
            closed_at=NOW,
            is_shadow=True,
        )
        store = PortfolioStore(session_factory(db), mode=ExecutionMode.PAPER)

        assert await store.attempts_closed_between(VENUE, None, NOW + INTERVAL) == []
        assert await store.live_attempts(VENUE) == []


class TestDailyBoundaryAndStreaks:
    async def test_the_daily_window_starts_at_midnight_utc(self) -> None:
        assert utc_day_start(datetime(2026, 9, 12, 23, 59, tzinfo=UTC)) == datetime(
            2026, 9, 12, tzinfo=UTC
        )

    async def test_yesterdays_loss_is_not_todays(
        self, db: AsyncSession, markets: dict[MarketRef, int]
    ) -> None:
        yesterday = NOW - timedelta(days=1)
        await complete_attempt(
            db,
            markets,
            attempt_id="yesterday",
            buy_exit="99000",
            sell_exit="100100",
            closed_at=yesterday,
        )
        source = PortfolioPnlSource(
            PortfolioStore(session_factory(db), mode=ExecutionMode.PAPER),
            venue=VENUE,
            clock=lambda: NOW,
        )

        reading = await source.realized_pnl_today_usd()

        assert reading is not None
        assert reading.trades == 0
        assert reading.net_usd == Decimal(0)
        assert reading.window_start == datetime(2026, 9, 12, tzinfo=UTC)

    async def test_todays_loss_is_counted_and_named_incomplete(
        self, db: AsyncSession, markets: dict[MarketRef, int]
    ) -> None:
        """Spot -1000, perp 0: the pair lost 1000 on price."""
        await complete_attempt(
            db, markets, attempt_id="today", buy_exit="99000", sell_exit="100100", closed_at=NOW
        )
        source = PortfolioPnlSource(
            PortfolioStore(session_factory(db), mode=ExecutionMode.PAPER),
            venue=VENUE,
            clock=lambda: NOW + timedelta(minutes=1),
        )

        reading = await source.realized_pnl_today_usd()

        assert reading is not None
        assert reading.net_usd == Decimal(-1000)
        assert reading.trades == 1
        assert FUNDING in reading.unmeasured
        assert not reading.is_complete

    async def test_consecutive_losses_count_pairs_not_legs(
        self, db: AsyncSession, markets: dict[MarketRef, int]
    ) -> None:
        """Each losing pair has one losing leg and one winning leg.

        Counting legs would report 6 for the three losses below, and the
        Phase 9 limit of 5 would fire on a book that had lost three times.
        """
        for index in range(3):
            await complete_attempt(
                db,
                markets,
                attempt_id=f"loss-{index}",
                buy_exit="99900",
                sell_exit="100100",
                closed_at=NOW + timedelta(minutes=index),
            )
        source = PortfolioPnlSource(
            PortfolioStore(session_factory(db), mode=ExecutionMode.PAPER),
            venue=VENUE,
            clock=lambda: NOW + timedelta(minutes=10),
        )

        assert await source.consecutive_losses() == 3

    async def test_a_win_breaks_the_streak(
        self, db: AsyncSession, markets: dict[MarketRef, int]
    ) -> None:
        await complete_attempt(
            db, markets, attempt_id="loss", buy_exit="99900", sell_exit="100100", closed_at=NOW
        )
        await complete_attempt(
            db,
            markets,
            attempt_id="win",
            buy_exit="100050",
            sell_exit="100050",
            closed_at=NOW + timedelta(minutes=1),
        )
        source = PortfolioPnlSource(
            PortfolioStore(session_factory(db), mode=ExecutionMode.PAPER),
            venue=VENUE,
            clock=lambda: NOW + timedelta(minutes=10),
        )

        assert await source.consecutive_losses() == 0

    async def test_inactivity_does_not_hide_an_unbroken_loss_streak(
        self, db: AsyncSession, markets: dict[MarketRef, int]
    ) -> None:
        for index in range(3):
            await complete_attempt(
                db,
                markets,
                attempt_id=f"old-loss-{index}",
                buy_exit="99900",
                sell_exit="100100",
                closed_at=NOW + timedelta(minutes=index),
            )
        source = PortfolioPnlSource(
            PortfolioStore(session_factory(db), mode=ExecutionMode.PAPER),
            venue=VENUE,
            streak_limit=3,
            clock=lambda: NOW + timedelta(days=60),
        )

        assert await source.consecutive_losses() == 3

    async def test_an_unreadable_database_reports_unavailable_not_zero(self) -> None:
        """Phase 9's fail-closed behaviour has to survive, unchanged."""

        class Broken(PortfolioStore):
            async def attempts_closed_between(self, venue, start, end):  # type: ignore[no-untyped-def]
                raise RuntimeError("database is gone")

        source = PortfolioPnlSource(
            Broken(session_factory(None), mode=ExecutionMode.PAPER),  # type: ignore[arg-type]
            venue=VENUE,
        )

        assert await source.realized_pnl_today_usd() is None
        assert await source.consecutive_losses() is None
        assert source.read_failures == 1


class TestRiskLimitsNowFire:
    async def build_engine(
        self, db: AsyncSession, source: PortfolioPnlSource, risk: RiskConfig
    ) -> RiskEngine:
        store = RiskEventStore(session_factory(db))
        switch = KillSwitchState(
            store, session_factory(db), mode=ExecutionMode.PAPER, clock=lambda: NOW
        )
        await switch.load()
        account = PaperAccount(ExecutionConfig(), risk)
        return RiskEngine(
            risk,
            account,
            store,
            switch,
            shadow_account=account,
            pnl_source=source,
            mode=ExecutionMode.PAPER,
            clock=lambda: NOW,
        )

    async def test_the_daily_loss_limit_genuinely_halts_trading(
        self, db: AsyncSession, markets: dict[MarketRef, int]
    ) -> None:
        """Phase 9 could not fire this. With real P&L behind it, it does."""
        await complete_attempt(
            db,
            markets,
            attempt_id="big-loss",
            buy_exit="99000",
            sell_exit="100100",
            closed_at=NOW - timedelta(minutes=1),
        )
        source = PortfolioPnlSource(
            PortfolioStore(session_factory(db), mode=ExecutionMode.PAPER),
            venue=VENUE,
            clock=lambda: NOW,
        )
        engine = await self.build_engine(db, source, RiskConfig(max_daily_loss_usd=200))

        verdict = await engine.evaluate(
            fresh_signal(), intent_id="signal:x", opportunity_uid=None, is_shadow=False
        )

        assert verdict.decision is RiskDecision.PAUSED
        assert verdict.draft.event_type is RiskEventType.DAILY_LOSS_LIMIT
        assert verdict.draft.observed_value == Decimal(-1000)
        # And it halted trading, rather than only refusing this one signal.
        assert engine.halted_reason() is not None

    async def test_the_consecutive_loss_limit_genuinely_pauses_trading(
        self, db: AsyncSession, markets: dict[MarketRef, int]
    ) -> None:
        for index in range(3):
            await complete_attempt(
                db,
                markets,
                attempt_id=f"loss-{index}",
                buy_exit="99990",
                sell_exit="100100",
                closed_at=NOW - timedelta(minutes=index + 1),
            )
        source = PortfolioPnlSource(
            PortfolioStore(session_factory(db), mode=ExecutionMode.PAPER),
            venue=VENUE,
            clock=lambda: NOW,
        )
        engine = await self.build_engine(
            db, source, RiskConfig(max_daily_loss_usd=100000, max_consecutive_losses=3)
        )

        verdict = await engine.evaluate(
            fresh_signal(), intent_id="signal:x", opportunity_uid=None, is_shadow=False
        )

        assert verdict.draft.event_type is RiskEventType.CONSECUTIVE_LOSSES
        assert verdict.draft.context is not None
        assert verdict.draft.context["requires_rearm"] is True

    async def test_an_approval_names_the_pnl_components_it_could_not_measure(
        self, db: AsyncSession, markets: dict[MarketRef, int]
    ) -> None:
        await complete_attempt(
            db,
            markets,
            attempt_id="small",
            buy_exit="100050",
            sell_exit="100050",
            closed_at=NOW - timedelta(minutes=1),
        )
        source = PortfolioPnlSource(
            PortfolioStore(session_factory(db), mode=ExecutionMode.PAPER),
            venue=VENUE,
            clock=lambda: NOW,
        )
        engine = await self.build_engine(db, source, RiskConfig())

        verdict = await engine.evaluate(
            fresh_signal(), intent_id="signal:x", opportunity_uid=None, is_shadow=False
        )

        assert verdict.is_approved
        assert verdict.draft.context is not None
        assert verdict.draft.context["incomplete_pnl_components"] == [FUNDING]
        # The two controls are no longer deferred: they were evaluated.
        assert verdict.draft.context["deferred_controls"] == []


class TestSnapshots:
    async def test_cash_is_replayed_from_the_fills(
        self, db: AsyncSession, markets: dict[MarketRef, int]
    ) -> None:
        """100000 start, one spot buy of 100000 at a fee of 1 per fill.

        The perpetual leg moves no cash - its margin is reserved rather than
        spent - so cash is 100000 - 100000 - 2 = -2.
        """
        await open_attempt(db, markets, fee="1")

        assert await writer(db).cash() == Decimal(-2)

    async def test_a_shadow_probes_fills_never_move_cash(
        self, db: AsyncSession, markets: dict[MarketRef, int]
    ) -> None:
        await open_attempt(db, markets, attempt_id="probe", is_shadow=True, fee="1")

        assert await writer(db).cash() == Decimal(100000)

    async def test_closed_perpetual_pnl_settles_into_durable_cash(
        self, db: AsyncSession, markets: dict[MarketRef, int]
    ) -> None:
        """Spot makes 50 and the short perpetual makes 50: cash gains 100."""
        await complete_attempt(
            db,
            markets,
            attempt_id="cash-pnl",
            buy_exit="100050",
            sell_exit="100050",
            closed_at=NOW,
        )

        assert await writer(db).cash() == Decimal(100100)

    async def test_a_snapshot_is_upserted_rather_than_duplicated(
        self, db: AsyncSession, markets: dict[MarketRef, int]
    ) -> None:
        state = value_book(
            [],
            StubMarks(),  # type: ignore[arg-type]
            cash_usd=Decimal(100000),
            realized_pnl_usd=Decimal(0),
            captured_at=NOW,
        )
        snapshots = writer(db)

        await snapshots.write_portfolio(state)
        await snapshots.write_portfolio(state)

        count = await db.scalar(select(func.count(PortfolioSnapshot.id)))
        assert count == 1

    async def test_an_unpriceable_book_publishes_no_equity_at_all(
        self, db: AsyncSession, markets: dict[MarketRef, int]
    ) -> None:
        await open_attempt(db, markets)
        store = PortfolioStore(session_factory(db), mode=ExecutionMode.PAPER)
        attempts = await store.live_attempts(VENUE)

        state = value_book(
            attempts,
            StubMarks(),  # nothing priced
            cash_usd=Decimal(100000),
            realized_pnl_usd=Decimal(0),
            captured_at=NOW,
        )
        await writer(db).write_portfolio(state)

        row = (await db.execute(select(PortfolioSnapshot))).scalars().one()
        assert row.valuation_status is ValuationStatus.UNAVAILABLE
        assert row.equity_usd is None
        assert row.position_value_usd is None
        assert row.unvalued_positions == 2

    async def test_a_partly_priceable_book_publishes_no_partial_equity(
        self, db: AsyncSession, markets: dict[MarketRef, int]
    ) -> None:
        await open_attempt(db, markets)
        store = PortfolioStore(session_factory(db), mode=ExecutionMode.PAPER)
        attempts = await store.live_attempts(VENUE)

        state = value_book(
            attempts,
            StubMarks(spot="100050"),  # type: ignore[arg-type]
            cash_usd=Decimal(100000),
            realized_pnl_usd=Decimal(0),
            captured_at=NOW,
        )

        assert state.valuation_status is ValuationStatus.UNAVAILABLE
        assert state.unvalued_positions == 1
        assert state.position_value_usd is None
        assert state.unrealized_pnl_usd is None
        assert state.equity_usd is None

    async def test_equity_is_cash_plus_the_executable_position_value(
        self, db: AsyncSession, markets: dict[MarketRef, int]
    ) -> None:
        """Spot long worth 100050 to sell; perp short is 50 down on the mark.

        cash = 100000 - 100000 = 0; position value = 100050 - 50 = 100000.
        """
        await open_attempt(db, markets)
        store = PortfolioStore(session_factory(db), mode=ExecutionMode.PAPER)
        attempts = await store.live_attempts(VENUE)

        state = value_book(
            attempts,
            StubMarks(spot="100050", perpetual="100150"),  # type: ignore[arg-type]
            cash_usd=await writer(db).cash(),
            realized_pnl_usd=Decimal(0),
            captured_at=NOW,
        )

        assert state.cash_usd == Decimal(0)
        assert state.position_value_usd == Decimal(100000)
        assert state.equity_usd == Decimal(100000)
        assert state.valuation_status is ValuationStatus.COMPLETE

    async def test_an_unpaired_position_is_counted_on_the_snapshot(
        self, db: AsyncSession, markets: dict[MarketRef, int]
    ) -> None:
        ids = await open_attempt(db, markets)
        perp = await db.get(Position, ids[PERP])
        assert perp is not None
        perp.status = PositionStatus.CLOSED
        perp.closed_quantity = perp.quantity
        perp.closed_at = NOW
        perp.exit_price = Decimal(100050)
        await db.flush()
        store = PortfolioStore(session_factory(db), mode=ExecutionMode.PAPER)

        state = value_book(
            await store.live_attempts(VENUE),
            StubMarks(spot="100050"),  # type: ignore[arg-type]
            cash_usd=Decimal(0),
            realized_pnl_usd=Decimal(0),
            captured_at=NOW,
        )

        assert state.unpaired_positions == 1


class TestPnlSnapshots:
    async def test_pnl_rows_are_upserted_per_window_and_scope(
        self, db: AsyncSession, markets: dict[MarketRef, int]
    ) -> None:
        await complete_attempt(
            db, markets, attempt_id="t1", buy_exit="100050", sell_exit="100050", closed_at=NOW
        )
        store = PortfolioStore(session_factory(db), mode=ExecutionMode.PAPER)
        snapshots = writer(db)
        trades = [
            attempt.paired()
            for attempt in await store.attempts_closed_between(VENUE, None, NOW + INTERVAL)
        ]
        window = Window("1d", utc_day_start(NOW), NOW + INTERVAL)
        row = snapshots.pnl_row(window=window, captured_at=NOW, trades=trades, curve=())

        await snapshots.write_pnl([row])
        await snapshots.write_pnl([row])

        count = await db.scalar(select(func.count(PnlSnapshot.id)))
        assert count == 1
        stored = (await db.execute(select(PnlSnapshot))).scalars().one()
        assert stored.scope_key == "portfolio"
        assert stored.trade_count == 1
        assert stored.winning_trades == 1
        assert stored.realized_pnl_usd == Decimal(100)
        assert stored.window_start == utc_day_start(NOW)

    async def test_ratios_stay_null_without_enough_observations(
        self, db: AsyncSession, markets: dict[MarketRef, int]
    ) -> None:
        snapshots = writer(db)
        row = snapshots.pnl_row(
            window=Window("all", None, NOW),
            captured_at=NOW,
            trades=(),
            curve=[(NOW, Decimal(100)), (NOW + INTERVAL, Decimal(101))],
        )

        await snapshots.write_pnl([row])

        stored = (await db.execute(select(PnlSnapshot))).scalars().one()
        assert stored.sharpe_ratio is None
        assert stored.sortino_ratio is None
        # The sampling assumption is stated even when the ratios are not.
        assert stored.return_interval_seconds == 60
        assert stored.return_observations == 1

    async def test_funding_is_null_on_the_row_and_named_as_unmeasured(
        self, db: AsyncSession, markets: dict[MarketRef, int]
    ) -> None:
        await complete_attempt(
            db, markets, attempt_id="t1", buy_exit="100050", sell_exit="100050", closed_at=NOW
        )
        store = PortfolioStore(session_factory(db), mode=ExecutionMode.PAPER)
        snapshots = writer(db)
        trades = [
            attempt.paired()
            for attempt in await store.attempts_closed_between(VENUE, None, NOW + INTERVAL)
        ]

        await snapshots.write_pnl(
            [
                snapshots.pnl_row(
                    window=Window("all", None, NOW), captured_at=NOW, trades=trades, curve=()
                )
            ]
        )

        stored = (await db.execute(select(PnlSnapshot))).scalars().one()
        assert stored.funding_pnl_usd is None
        assert stored.unmeasured_pnl == [FUNDING]


class TestServiceSnapshot:
    async def test_open_strategy_gets_its_own_unrealized_pnl_rows(
        self, db: AsyncSession, markets: dict[MarketRef, int]
    ) -> None:
        await open_attempt(db, markets, attempt_id="open")
        store = PortfolioStore(session_factory(db), mode=ExecutionMode.PAPER)
        service = PortfolioService(
            store=store,
            writer=writer(db),
            marks=StubMarks(spot="100050", perpetual="100050"),  # type: ignore[arg-type]
            closer=None,
            pnl_source=None,
            config=PortfolioConfig(enabled=True, snapshot_interval_ms=60_000),
            venue=VENUE,
            clock=lambda: NOW + timedelta(seconds=30),
        )

        await service.snapshot()

        rows = (
            (
                await db.execute(
                    select(PnlSnapshot).where(PnlSnapshot.scope_key == "strategy:spot_perp_basis")
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 3
        assert {row.unrealized_pnl_usd for row in rows} == {Decimal(100)}

    async def test_a_full_snapshot_writes_both_tables_and_is_idempotent(
        self, db: AsyncSession, markets: dict[MarketRef, int]
    ) -> None:
        await complete_attempt(
            db, markets, attempt_id="t1", buy_exit="100050", sell_exit="100050", closed_at=NOW
        )
        store = PortfolioStore(session_factory(db), mode=ExecutionMode.PAPER)
        service = PortfolioService(
            store=store,
            writer=writer(db),
            marks=StubMarks(spot="100050", perpetual="100050"),  # type: ignore[arg-type]
            closer=None,
            pnl_source=None,
            config=PortfolioConfig(enabled=True, snapshot_interval_ms=60_000),
            venue=VENUE,
            clock=lambda: NOW + timedelta(seconds=30),
        )

        await service.snapshot()
        portfolio_rows = await db.scalar(select(func.count(PortfolioSnapshot.id)))
        pnl_rows = await db.scalar(select(func.count(PnlSnapshot.id)))
        await service.snapshot()

        assert await db.scalar(select(func.count(PortfolioSnapshot.id))) == portfolio_rows
        assert await db.scalar(select(func.count(PnlSnapshot.id))) == pnl_rows
        assert portfolio_rows == 1
        # Portfolio and per-strategy rows for three windows, plus one
        # all-time row per closed position.
        assert pnl_rows == 8


class TestHealth:
    def settings(self, **overrides: object) -> Settings:
        return Settings(portfolio=PortfolioConfig(**overrides))  # type: ignore[arg-type]

    async def test_disabled_reads_offline_and_says_what_that_costs(self) -> None:
        health = await portfolio_status(self.settings(enabled=False), NOW)

        assert health.status is ComponentStatus.OFFLINE
        assert "switched off" in health.detail
        assert "deferred" in health.detail

    async def test_enabled_but_never_run_is_offline_not_healthy(
        self, db: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "trading_bot.api.portfolio_status.get_session_factory",
            lambda: session_factory(db),
        )

        health = await portfolio_status(self.settings(enabled=True), NOW)

        assert health.status is ComponentStatus.OFFLINE
        assert "has ever been written" in health.detail

    async def test_a_recent_complete_snapshot_is_healthy(
        self, db: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "trading_bot.api.portfolio_status.get_session_factory",
            lambda: session_factory(db),
        )
        db.add(
            PortfolioSnapshot(
                captured_at=NOW,
                mode=ExecutionMode.PAPER,
                cash_usd=Decimal(100000),
                equity_usd=Decimal(100000),
                position_value_usd=Decimal(0),
                valuation_status=ValuationStatus.COMPLETE,
            )
        )
        await db.flush()

        health = await portfolio_status(self.settings(enabled=True), NOW)

        assert health.status is ComponentStatus.HEALTHY
        assert "equity 100000.00 USD" in health.detail

    async def test_a_stale_snapshot_is_degraded(
        self, db: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "trading_bot.api.portfolio_status.get_session_factory",
            lambda: session_factory(db),
        )
        db.add(
            PortfolioSnapshot(
                captured_at=NOW - timedelta(hours=1),
                mode=ExecutionMode.PAPER,
                cash_usd=Decimal(100000),
                equity_usd=Decimal(100000),
                position_value_usd=Decimal(0),
            )
        )
        await db.flush()

        health = await portfolio_status(self.settings(enabled=True), NOW)

        assert health.status is ComponentStatus.DEGRADED
        assert "not being updated" in health.detail

    async def test_naked_exposure_is_reported_loudly(
        self, db: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "trading_bot.api.portfolio_status.get_session_factory",
            lambda: session_factory(db),
        )
        db.add(
            PortfolioSnapshot(
                captured_at=NOW,
                mode=ExecutionMode.PAPER,
                cash_usd=Decimal(100000),
                equity_usd=Decimal(100000),
                position_value_usd=Decimal(0),
                open_positions=1,
                unpaired_positions=1,
            )
        )
        await db.flush()

        health = await portfolio_status(self.settings(enabled=True), NOW)

        assert health.status is ComponentStatus.DEGRADED
        assert "hedge no longer exists" in health.detail

    async def test_an_unavailable_valuation_is_degraded_not_healthy(
        self, db: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "trading_bot.api.portfolio_status.get_session_factory",
            lambda: session_factory(db),
        )
        db.add(
            PortfolioSnapshot(
                captured_at=NOW,
                mode=ExecutionMode.PAPER,
                cash_usd=Decimal(100000),
                equity_usd=None,
                position_value_usd=None,
                open_positions=2,
                unvalued_positions=2,
                valuation_status=ValuationStatus.UNAVAILABLE,
            )
        )
        await db.flush()

        health = await portfolio_status(self.settings(enabled=True), NOW)

        assert health.status is ComponentStatus.DEGRADED
        assert "equity is unavailable" in health.detail

    async def test_incomplete_accounting_is_degraded(
        self, db: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "trading_bot.api.portfolio_status.get_session_factory",
            lambda: session_factory(db),
        )
        db.add(
            PortfolioSnapshot(
                captured_at=NOW,
                mode=ExecutionMode.PAPER,
                cash_usd=Decimal(100000),
                equity_usd=Decimal(100000),
                position_value_usd=Decimal(0),
            )
        )
        db.add(
            PnlSnapshot(
                captured_at=NOW,
                mode=ExecutionMode.PAPER,
                scope_key="portfolio",
                window="all",
                unmeasured_pnl=[FUNDING],
            )
        )
        await db.flush()

        health = await portfolio_status(self.settings(enabled=True), NOW)

        assert health.status is ComponentStatus.DEGRADED
        assert "not a total" in health.detail

    async def test_an_unreadable_database_never_raises(
        self, db: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def broken() -> None:
            raise RuntimeError("no engine")

        monkeypatch.setattr("trading_bot.api.portfolio_status.get_session_factory", broken)

        health = await portfolio_status(self.settings(enabled=True), NOW)

        assert health.status is ComponentStatus.OFFLINE
        assert "cannot read" in health.detail
