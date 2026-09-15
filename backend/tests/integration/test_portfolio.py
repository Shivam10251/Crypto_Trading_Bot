"""Closing positions against real PostgreSQL.

PostgreSQL is the point: the reduce-only CHECK constraint, the claim two
workers cannot both take, the unique indexes a retried close converges on, and
the row-locked recomputation that makes a crash mid-exit recoverable. None of
those are properties of the Python; they are properties of the database, and a
fake would agree with the code rather than test it.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tests.integration.factories import make_market
from trading_bot.core.config import ExecutionConfig, ExitPolicyConfig, RiskConfig
from trading_bot.db.models import Fill, Market, Order, Position
from trading_bot.db.models.enums import (
    ExecutionMode,
    MarketType,
    OrderStatus,
    OrderType,
    PositionStatus,
    Side,
)
from trading_bot.exchange.models import MarketRef
from trading_bot.execution.account import PaperAccount
from trading_bot.execution.models import (
    ExecutionResult,
    OrderIntent,
    OrderRequest,
    RejectionCode,
    SimulatedFill,
)
from trading_bot.portfolio import exits
from trading_bot.portfolio.closer import PositionCloser
from trading_bot.portfolio.store import ClaimLost, PortfolioStore, close_intent_id
from trading_bot.risk.engine import RiskEngine
from trading_bot.risk.kill_switch import KillSwitchState
from trading_bot.risk.store import RiskEventStore

pytestmark = pytest.mark.requires_postgres

NOW = datetime(2026, 9, 12, 12, 0, tzinfo=UTC)
VENUE = "binance"
SPOT = MarketRef(VENUE, "BTCUSDT", MarketType.SPOT)
PERP = MarketRef(VENUE, "BTCUSDT", MarketType.PERPETUAL)

SessionFactory = Callable[[], contextlib.AbstractAsyncContextManager[AsyncSession]]


def session_factory(db: AsyncSession) -> SessionFactory:
    @contextlib.asynccontextmanager
    async def factory() -> AsyncIterator[AsyncSession]:
        yield db

    return factory


@pytest.fixture
async def committed(postgres_url: str) -> AsyncIterator[SessionFactory]:
    """Independent sessions that really commit, for the concurrency tests."""
    engine = create_async_engine(postgres_url)
    maker = async_sessionmaker(engine, expire_on_commit=False)

    @contextlib.asynccontextmanager
    async def factory() -> AsyncIterator[AsyncSession]:
        async with maker() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    try:
        yield factory
    finally:
        async with maker() as session:
            for table in (Fill, Order, Position, Market):
                await session.execute(delete(table))
            await session.commit()
        await engine.dispose()


@pytest.fixture
async def markets(db: AsyncSession) -> dict[MarketRef, int]:
    spot = make_market("BTCUSDT", MarketType.SPOT)
    perp = make_market("BTCUSDT", MarketType.PERPETUAL)
    db.add_all([spot, perp])
    await db.flush()
    return {SPOT: spot.id, PERP: perp.id}


async def open_attempt(
    session: AsyncSession,
    markets: dict[MarketRef, int],
    *,
    attempt_id: str = "attempt-1",
    buy_price: str = "100000",
    sell_price: str = "100100",
    quantity: str = "1",
    fee: str = "0",
    mode: ExecutionMode = ExecutionMode.PAPER,
    is_shadow: bool = False,
) -> dict[MarketRef, int]:
    """An entered basis attempt: both legs' orders, fills and positions."""
    position_ids: dict[MarketRef, int] = {}
    for index, (ref, side, price) in enumerate(
        ((SPOT, Side.BUY, buy_price), (PERP, Side.SELL, sell_price))
    ):
        position = Position(
            market_id=markets[ref],
            attempt_id=attempt_id,
            is_shadow=is_shadow,
            mode=mode,
            strategy="spot_perp_basis",
            side=side,
            status=PositionStatus.OPEN,
            quantity=Decimal(quantity),
            entry_price=Decimal(price),
            entry_notional_usd=Decimal(price) * Decimal(quantity),
            fees_usd=Decimal(fee),
            opened_at=NOW,
        )
        session.add(position)
        await session.flush()
        order = Order(
            market_id=markets[ref],
            mode=mode,
            is_shadow=is_shadow,
            attempt_id=attempt_id,
            execution_intent_id=f"signal:{attempt_id}",
            signal_leg=index,
            intent=OrderIntent.OPEN.value,
            strategy="spot_perp_basis",
            client_order_id=f"{attempt_id}-{index}",
            side=side,
            order_type=OrderType.MARKET,
            quantity=Decimal(quantity),
            expected_price=Decimal(price),
            filled_quantity=Decimal(quantity),
            average_fill_price=Decimal(price),
            status=OrderStatus.FILLED,
            submitted_at=NOW,
        )
        session.add(order)
        await session.flush()
        session.add(
            Fill(
                order_id=order.id,
                position_id=position.id,
                mode=mode,
                price=Decimal(price),
                quantity=Decimal(quantity),
                fee_usd=Decimal(fee),
                filled_at=NOW,
                fill_index=0,
            )
        )
        position_ids[ref] = position.id
    await session.flush()
    return position_ids


class StubAdapter:
    """An adapter whose answer per market the test dictates.

    Keyed by symbol so a test can make one leg fill and the other refuse -
    the case that leaves real naked exposure behind a close.
    """

    def __init__(self, **prices: str | None) -> None:
        self.prices = prices
        self.submitted: list[OrderRequest] = []

    async def submit(self, request: OrderRequest) -> ExecutionResult:
        self.submitted.append(request)
        price = self.prices.get(request.ref.market_type.value.lower())
        if price is None:
            return ExecutionResult(
                request=request,
                status=OrderStatus.REJECTED,
                fills=(),
                submitted_at=NOW,
                acknowledged_at=NOW,
                closed_at=NOW,
                latency_ms=1,
                rejection=RejectionCode.NO_LIQUIDITY,
                detail="stub refused",
            )
        return ExecutionResult(
            request=request,
            status=OrderStatus.FILLED,
            fills=(
                SimulatedFill(
                    price=Decimal(price),
                    quantity=request.quantity,
                    filled_at=NOW + timedelta(minutes=10),
                    is_maker=False,
                    fee_usd=Decimal("1"),
                ),
            ),
            submitted_at=NOW,
            acknowledged_at=NOW,
            closed_at=NOW,
            latency_ms=1,
        )

    async def cancel(self, client_order_id: str):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    async def status(self, client_order_id: str) -> ExecutionResult | None:
        return None


class StubMarks:
    """Executable exits the test dictates, bypassing the book walk."""

    def __init__(self, **prices: str | None) -> None:
        self.prices = prices

    def executable_exit(self, ref: MarketRef, *, entry_side: Side, quantity: Decimal):  # type: ignore[no-untyped-def]
        from trading_bot.portfolio.valuation import ExecutableExit, exit_side

        price = self.prices.get(ref.market_type.value.lower())
        return ExecutableExit(
            ref=ref,
            side=exit_side(entry_side),
            quantity=quantity,
            price=Decimal(price) if price is not None else None,
            fillable=quantity if price is not None else Decimal(0),
            complete=price is not None,
            problem=None if price is not None else "NO_FEED",
        )


async def build_risk(factory: SessionFactory) -> RiskEngine:
    store = RiskEventStore(factory)
    switch = KillSwitchState(store, factory, mode=ExecutionMode.PAPER)
    await switch.load()
    account = PaperAccount(ExecutionConfig(), RiskConfig())
    return RiskEngine(
        RiskConfig(), account, store, switch, shadow_account=account, mode=ExecutionMode.PAPER
    )


def build_closer(
    factory: SessionFactory,
    risk: RiskEngine,
    markets: dict[MarketRef, int],
    *,
    adapter: StubAdapter | None = None,
    marks: StubMarks | None = None,
    account: PaperAccount | None = None,
    config: ExitPolicyConfig | None = None,
) -> PositionCloser:
    return PositionCloser(
        store=PortfolioStore(factory, mode=ExecutionMode.PAPER),
        session_factory=factory,
        adapter=adapter or StubAdapter(spot="100050", perpetual="100050"),  # type: ignore[arg-type]
        risk=risk,
        marks=marks or StubMarks(spot="100050", perpetual="100050"),  # type: ignore[arg-type]
        account=account,
        config=config or ExitPolicyConfig(enabled=True, target_basis_bps=1.0),
        venue=VENUE,
        clock=lambda: NOW + timedelta(minutes=5),
    )


class TestReduceOnlyIsADatabaseConstraint:
    async def test_closing_more_than_was_opened_is_refused_by_postgresql(
        self, db: AsyncSession, markets: dict[MarketRef, int]
    ) -> None:
        ids = await open_attempt(db, markets)
        position = await db.get(Position, ids[SPOT])
        assert position is not None

        position.closed_quantity = Decimal(2)
        with pytest.raises(IntegrityError):
            await db.flush()

    async def test_a_closed_position_must_be_flat(
        self, db: AsyncSession, markets: dict[MarketRef, int]
    ) -> None:
        ids = await open_attempt(db, markets)
        position = await db.get(Position, ids[SPOT])
        assert position is not None

        position.status = PositionStatus.CLOSED
        position.closed_at = NOW
        position.exit_price = Decimal(100)
        position.closed_quantity = Decimal("0.5")
        with pytest.raises(IntegrityError):
            await db.flush()


class TestClosingAnAttempt:
    async def test_a_converged_basis_closes_both_legs_and_realizes_pnl(
        self, db: AsyncSession, markets: dict[MarketRef, int]
    ) -> None:
        """Bought 100000, sold 100100; both closed at 100050.

        Spot: (100050 - 100000) = +50. Perp: (100100 - 100050) = +50.
        Net +100 of price P&L, less 2 of exit fees (1 per leg).
        """
        factory = session_factory(db)
        await open_attempt(db, markets)
        closer = build_closer(factory, await build_risk(factory), markets)

        outcomes = await closer.sweep()

        assert len(outcomes) == 1
        assert outcomes[0].reason is exits.ExitReason.BASIS_CONVERGED
        assert len(outcomes[0].closed_positions) == 2
        rows = (await db.execute(select(Position))).scalars().all()
        assert all(row.status is PositionStatus.CLOSED for row in rows)
        assert sum(row.realized_pnl_usd for row in rows) == Decimal(98)
        assert sum(row.exit_fees_usd for row in rows) == Decimal(2)

    async def test_close_orders_carry_the_close_intent(
        self, db: AsyncSession, markets: dict[MarketRef, int]
    ) -> None:
        factory = session_factory(db)
        await open_attempt(db, markets)
        closer = build_closer(factory, await build_risk(factory), markets)

        await closer.sweep()

        closes = (
            (await db.execute(select(Order).where(Order.intent == OrderIntent.CLOSE.value)))
            .scalars()
            .all()
        )
        assert len(closes) == 2
        assert {order.side for order in closes} == {Side.SELL, Side.BUY}
        assert all(order.execution_intent_id == close_intent_id("attempt-1", 0) for order in closes)

    async def test_both_legs_are_submitted_before_either_is_awaited(
        self, db: AsyncSession, markets: dict[MarketRef, int]
    ) -> None:
        """Unwinding one leg and then the other leaves the pair unhedged in
        between - the same reason entries go out together."""
        factory = session_factory(db)
        await open_attempt(db, markets)
        order: list[str] = []

        class Ordered(StubAdapter):
            async def submit(self, request: OrderRequest) -> ExecutionResult:
                order.append(f"start:{request.ref.market_type.value}")
                await asyncio.sleep(0)
                order.append(f"end:{request.ref.market_type.value}")
                return await super().submit(request)

        closer = build_closer(
            factory,
            await build_risk(factory),
            markets,
            adapter=Ordered(spot="100050", perpetual="100050"),
        )

        await closer.sweep()

        assert order[0].startswith("start") and order[1].startswith("start")

    async def test_a_close_that_fills_one_leg_leaves_real_naked_exposure(
        self, db: AsyncSession, markets: dict[MarketRef, int]
    ) -> None:
        factory = session_factory(db)
        ids = await open_attempt(db, markets)
        closer = build_closer(
            factory,
            await build_risk(factory),
            markets,
            adapter=StubAdapter(spot="100050", perpetual=None),
        )

        outcomes = await closer.sweep()

        assert outcomes[0].left_unhedged
        spot = await db.get(Position, ids[SPOT])
        perp = await db.get(Position, ids[PERP])
        assert spot is not None and perp is not None
        assert spot.status is PositionStatus.CLOSED
        # A terminal partial attempt releases the residual immediately; it
        # does not sit behind the stale-claim timeout before the next retry.
        assert perp.status is PositionStatus.OPEN
        assert perp.closed_quantity == Decimal(0)

    async def test_residual_exposure_is_closed_on_the_next_sweep(
        self, db: AsyncSession, markets: dict[MarketRef, int]
    ) -> None:
        """The naked leg is picked up as UNPAIRED_RESIDUAL, not left behind."""
        factory = session_factory(db)
        await open_attempt(db, markets)
        risk = await build_risk(factory)
        build_closer(factory, risk, markets, adapter=StubAdapter(spot="100050", perpetual=None))
        await build_closer(
            factory, risk, markets, adapter=StubAdapter(spot="100050", perpetual=None)
        ).sweep()

        second = build_closer(factory, risk, markets)
        outcomes = await second.sweep()

        assert outcomes[0].reason is exits.ExitReason.UNPAIRED_RESIDUAL

    async def test_the_account_gets_its_exposure_back(
        self, db: AsyncSession, markets: dict[MarketRef, int]
    ) -> None:
        from trading_bot.execution.account import PaperPositionSeed

        factory = session_factory(db)
        await open_attempt(db, markets, quantity="0.001")
        account = PaperAccount(ExecutionConfig(), RiskConfig())
        account.restore(
            [
                PaperPositionSeed(
                    "BTCUSDT",
                    MarketType.SPOT,
                    Side.BUY,
                    Decimal("0.001"),
                    Decimal(100),
                    Decimal(0),
                ),
                PaperPositionSeed(
                    "BTCUSDT",
                    MarketType.PERPETUAL,
                    Side.SELL,
                    Decimal("0.001"),
                    Decimal(100),
                    Decimal(0),
                ),
            ]
        )
        assert account.gross_exposure_usd == Decimal(200)
        closer = build_closer(factory, await build_risk(factory), markets, account=account)

        await closer.sweep()

        assert account.gross_exposure_usd == Decimal(0)


class TestIdempotencyAndRecovery:
    async def test_a_retried_close_does_not_duplicate_orders_or_fills(
        self, db: AsyncSession, markets: dict[MarketRef, int]
    ) -> None:
        """The close's identity is deterministic, so a replay converges.

        This is the crash-after-submit case: the orders and fills landed, the
        acknowledgement was lost, and the same close is written again. Both
        unique indexes - ``(mode, client_order_id)`` and
        ``(order_id, fill_index)`` - have to turn that into an update.
        """
        factory = session_factory(db)
        await open_attempt(db, markets)
        store = PortfolioStore(factory, mode=ExecutionMode.PAPER)
        adapter = StubAdapter(spot="100050", perpetual="100050")
        closer = build_closer(factory, await build_risk(factory), markets, adapter=adapter)
        attempts = await store.live_attempts(VENUE)
        intent = close_intent_id("attempt-1", 0)
        legs = list(attempts[0].live_legs)
        requests = [
            OrderRequest(
                ref=leg.ref,
                side=Side.SELL if leg.side is Side.BUY else Side.BUY,
                quantity=leg.open_quantity,
                order_type=OrderType.MARKET,
                intent=OrderIntent.CLOSE,
                client_order_id=f"{intent}-{index}",
                execution_intent_id=intent,
                attempt_id=leg.attempt_id,
                strategy=leg.strategy,
                signal_leg=index,
            )
            for index, leg in enumerate(legs)
        ]
        results = [await adapter.submit(request) for request in requests]

        await closer._record(legs, requests, results, attempts[0], intent, NOW)
        await closer._record(legs, requests, results, attempts[0], intent, NOW)

        closes = (
            (await db.execute(select(Order).where(Order.intent == OrderIntent.CLOSE.value)))
            .scalars()
            .all()
        )
        fills = (await db.execute(select(Fill))).scalars().all()
        assert len(closes) == 2
        # Two entry fills plus two close fills, not four closes.
        assert len(fills) == 4
        rows = (await db.execute(select(Position))).scalars().all()
        assert all(row.closed_quantity == Decimal(1) for row in rows)

    async def test_reconcile_is_idempotent(
        self, db: AsyncSession, markets: dict[MarketRef, int]
    ) -> None:
        factory = session_factory(db)
        await open_attempt(db, markets)
        store = PortfolioStore(factory, mode=ExecutionMode.PAPER)
        closer = build_closer(factory, await build_risk(factory), markets)
        await closer.sweep()
        rows = (await db.execute(select(Position))).scalars().all()
        before = {row.id: row.realized_pnl_usd for row in rows}

        await store.reconcile(list(before), now=NOW)

        rows = (await db.execute(select(Position))).scalars().all()
        assert {row.id: row.realized_pnl_usd for row in rows} == before

    async def test_a_closed_position_is_never_recomputed_again(
        self, db: AsyncSession, markets: dict[MarketRef, int]
    ) -> None:
        """A settled result must survive a later run - and a later cost model."""
        factory = session_factory(db)
        ids = await open_attempt(db, markets)
        store = PortfolioStore(factory, mode=ExecutionMode.PAPER)
        await build_closer(factory, await build_risk(factory), markets).sweep()
        position = await db.get(Position, ids[SPOT])
        assert position is not None and position.status is PositionStatus.CLOSED
        position.realized_pnl_usd = Decimal("-999")
        await db.flush()

        await store.reconcile([ids[SPOT]], now=NOW)
        await db.refresh(position)

        assert position.realized_pnl_usd == Decimal("-999")

    async def test_fills_written_before_a_crash_are_recovered(
        self, db: AsyncSession, markets: dict[MarketRef, int]
    ) -> None:
        """A close whose fills landed but whose position update did not.

        Recomputing from fills is what makes this recoverable: nothing has to
        remember what a dead worker had already applied.
        """
        factory = session_factory(db)
        ids = await open_attempt(db, markets)
        store = PortfolioStore(factory, mode=ExecutionMode.PAPER)
        position = await db.get(Position, ids[SPOT])
        assert position is not None
        position.status = PositionStatus.CLOSING
        position.close_intent_id = close_intent_id("attempt-1", 0)
        position.close_claimed_at = NOW - timedelta(minutes=10)
        position.close_attempts = 1
        order = Order(
            market_id=markets[SPOT],
            mode=ExecutionMode.PAPER,
            attempt_id="attempt-1",
            execution_intent_id=close_intent_id("attempt-1", 0),
            signal_leg=0,
            intent=OrderIntent.CLOSE.value,
            client_order_id=f"{close_intent_id('attempt-1', 0)}-0",
            side=Side.SELL,
            order_type=OrderType.MARKET,
            quantity=Decimal(1),
            filled_quantity=Decimal(1),
            average_fill_price=Decimal(100050),
            status=OrderStatus.FILLED,
            submitted_at=NOW,
        )
        db.add(order)
        await db.flush()
        db.add(
            Fill(
                order_id=order.id,
                position_id=ids[SPOT],
                mode=ExecutionMode.PAPER,
                price=Decimal(100050),
                quantity=Decimal(1),
                fee_usd=Decimal(1),
                filled_at=NOW,
                fill_index=0,
            )
        )
        await db.flush()

        closed = await store.reconcile([ids[SPOT]], now=NOW)

        assert closed == [ids[SPOT]]
        # Flush before re-reading: the test's session is rollback-wrapped, so
        # the recomputation is pending rather than committed.
        await db.flush()
        await db.refresh(position)
        assert position.status is PositionStatus.CLOSED
        # (100050 - 100000) * 1, less the 1 of exit fee.
        assert position.realized_pnl_usd == Decimal(49)


class TestConcurrentClosing:
    async def test_two_workers_cannot_both_claim_the_same_attempt(
        self, committed: SessionFactory, db: AsyncSession
    ) -> None:
        """The loser matches nothing: PostgreSQL re-evaluates the predicate
        after taking the row lock, so the claim is genuinely atomic."""
        async with committed() as session:
            spot = make_market("BTCUSDT", MarketType.SPOT)
            perp = make_market("BTCUSDT", MarketType.PERPETUAL)
            session.add_all([spot, perp])
            await session.flush()
            market_ids = {SPOT: spot.id, PERP: perp.id}
            await open_attempt(session, market_ids)
        store = PortfolioStore(committed, mode=ExecutionMode.PAPER)
        attempts = await store.live_attempts(VENUE)

        async def claim() -> str | None:
            try:
                return await store.claim(
                    attempts[0],
                    reason="BASIS_CONVERGED",
                    now=NOW,
                    claim_timeout=timedelta(seconds=30),
                    claim_id=uuid.uuid4().hex[:32],
                )
            except ClaimLost:
                return None

        first, second = await asyncio.gather(claim(), claim())

        assert [first, second].count(None) == 1

    async def test_a_stale_claim_can_be_recovered_by_another_worker(
        self, committed: SessionFactory
    ) -> None:
        async with committed() as session:
            spot = make_market("BTCUSDT", MarketType.SPOT)
            perp = make_market("BTCUSDT", MarketType.PERPETUAL)
            session.add_all([spot, perp])
            await session.flush()
            await open_attempt(session, {SPOT: spot.id, PERP: perp.id})
        store = PortfolioStore(committed, mode=ExecutionMode.PAPER)
        attempts = await store.live_attempts(VENUE)
        await store.claim(
            attempts[0],
            reason="BASIS_CONVERGED",
            now=NOW - timedelta(minutes=10),
            claim_timeout=timedelta(seconds=30),
        )

        again = await store.live_attempts(VENUE)
        recovered = await store.claim(
            again[0],
            reason="UNPAIRED_RESIDUAL",
            now=NOW,
            claim_timeout=timedelta(seconds=30),
        )

        assert recovered is not None
        # A claim that never reached submission did not consume an attempt or
        # its deterministic identity.
        assert recovered.intent_id == close_intent_id("attempt-1", 0)

    async def test_a_claim_refuses_a_stale_open_quantity(
        self, db: AsyncSession, markets: dict[MarketRef, int]
    ) -> None:
        """A partial reconciliation cannot turn a stale request into an over-close."""
        await open_attempt(db, markets)
        store = PortfolioStore(session_factory(db), mode=ExecutionMode.PAPER)
        attempt = (await store.live_attempts(VENUE))[0]
        position = await db.get(Position, attempt.live_legs[0].position_id)
        assert position is not None
        position.closed_quantity = Decimal("0.5")
        await db.flush()

        with pytest.raises(ClaimLost):
            await store.claim(
                attempt,
                reason="BASIS_CONVERGED",
                now=NOW,
                claim_timeout=timedelta(seconds=30),
            )


class TestRiskInteraction:
    async def test_a_halted_kill_switch_does_not_stop_a_close(
        self, db: AsyncSession, markets: dict[MarketRef, int]
    ) -> None:
        """A kill stops new exposure. Refusing to reduce it would leave the
        account holding exactly the risk the halt was called for."""
        factory = session_factory(db)
        await open_attempt(db, markets)
        risk = await build_risk(factory)
        await risk._kill_switch.trigger(who="operator", reason="halt everything")
        assert risk.halted_reason() is not None

        outcomes = await build_closer(factory, risk, markets).sweep()

        assert outcomes[0].acted
        rows = (await db.execute(select(Position))).scalars().all()
        assert all(row.status is PositionStatus.CLOSED for row in rows)

    async def test_every_close_writes_its_own_decision(
        self, db: AsyncSession, markets: dict[MarketRef, int]
    ) -> None:
        from trading_bot.db.models import RiskEvent
        from trading_bot.db.models.enums import RiskEventType

        factory = session_factory(db)
        await open_attempt(db, markets)

        await build_closer(factory, await build_risk(factory), markets).sweep()

        events = (
            (
                await db.execute(
                    select(RiskEvent).where(RiskEvent.event_type == RiskEventType.POSITION_EXIT)
                )
            )
            .scalars()
            .all()
        )
        assert len(events) == 1
        assert events[0].intent_id == close_intent_id("attempt-1", 0)
        assert events[0].context is not None
        assert events[0].context["exit_reason"] == "BASIS_CONVERGED"

    async def test_a_close_that_leaves_a_naked_leg_pauses_new_entries(
        self, db: AsyncSession, markets: dict[MarketRef, int]
    ) -> None:
        """The same response an unhedged entry gets, from the other direction."""
        from trading_bot.db.models import RiskEvent
        from trading_bot.db.models.enums import RiskEventType

        factory = session_factory(db)
        await open_attempt(db, markets)
        risk = await build_risk(factory)
        assert risk.halted_reason() is None

        await build_closer(
            factory, risk, markets, adapter=StubAdapter(spot="100050", perpetual=None)
        ).sweep()

        assert risk.halted_reason() is not None
        events = (
            (
                await db.execute(
                    select(RiskEvent).where(
                        RiskEvent.event_type == RiskEventType.ABNORMAL_EXECUTION
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(events) == 1
        assert "naked exposure" in events[0].reason


class TestEntryRecorderCannotUndoAClose:
    async def test_a_late_entry_flush_never_reopens_a_closed_position(
        self, db: AsyncSession, markets: dict[MarketRef, int]
    ) -> None:
        """The execution recorder's upsert must not resurrect exposure.

        A retried entry flush landing after a close would otherwise write
        status OPEN, realized 0 and the full entry quantity back over a
        position Phase 10 had settled - exposure the account does not have.
        """
        from sqlalchemy.dialects.postgresql import insert

        factory = session_factory(db)
        ids = await open_attempt(db, markets)
        await build_closer(factory, await build_risk(factory), markets).sweep()
        position = await db.get(Position, ids[SPOT])
        assert position is not None and position.status is PositionStatus.CLOSED
        realized = position.realized_pnl_usd

        values = {
            "market_id": markets[SPOT],
            "attempt_id": "attempt-1",
            "is_shadow": False,
            "mode": ExecutionMode.PAPER,
            "strategy": "spot_perp_basis",
            "side": Side.BUY,
            "status": PositionStatus.OPEN,
            "quantity": Decimal(1),
            "entry_price": Decimal(100000),
            "entry_notional_usd": Decimal(100000),
            "realized_pnl_usd": Decimal(0),
            "opened_at": NOW,
        }
        statement = insert(Position).values([values])
        await db.execute(
            statement.on_conflict_do_update(
                index_elements=["mode", "attempt_id", "market_id"],
                index_where=Position.backtest_run_id.is_(None),
                set_={
                    key: getattr(statement.excluded, key)
                    for key in values
                    if key not in {"mode", "attempt_id", "market_id"}
                },
                where=(Position.status == PositionStatus.OPEN) & (Position.closed_quantity == 0),
            )
        )
        await db.flush()
        await db.refresh(position)

        assert position.status is PositionStatus.CLOSED
        assert position.realized_pnl_usd == realized
