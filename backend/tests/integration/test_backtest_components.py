"""The replay's supporting pieces against PostgreSQL: source, capture, snapshots, isolation."""

from __future__ import annotations

import asyncio
import random
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tests.integration.backtest_support import (
    PAIR,
    PERP,
    START,
    DatasetBuilder,
    cleanup,
    factory_for,
    run_async,
    seed,
)
from tests.integration.factories import make_market, make_opportunity
from tests.integration.test_portfolio import PERP as PNL_PERP
from tests.integration.test_portfolio import SPOT as PNL_SPOT
from tests.integration.test_portfolio_pnl import (
    INTERVAL,
    VENUE,
    complete_attempt,
    session_factory,
    writer,
)
from trading_bot.api.strategy_status import recent_opportunities
from trading_bot.core.config import CaptureConfig, DatabaseConfig, PortfolioConfig
from trading_bot.db.models import BacktestRun, FundingObservation, OrderBookSnapshot, PnlSnapshot
from trading_bot.db.models.enums import (
    BacktestRunStatus,
    ExecutionMode,
    MarketType,
    OpportunityStatus,
)
from trading_bot.db.session import dispose_engine, init_engine
from trading_bot.exchange.models import BookLevel, FundingInfo, MarketRef, OrderBook, Quote
from trading_bot.marketdata.capture import ReplayCaptureRecorder
from trading_bot.marketdata.models import BookStatus, FeedStatus, MarketSnapshot
from trading_bot.opportunities.recost import recost
from trading_bot.portfolio.service import PortfolioService
from trading_bot.portfolio.store import PortfolioStore
from trading_bot.strategy.fees import FeeSchedule

pytestmark = pytest.mark.requires_postgres


@pytest.fixture
def replay_data(postgres_url: str) -> Iterator[None]:
    cleanup()
    yield
    cleanup()


def postgres_url_value() -> str:
    from tests.integration.conftest import TEST_URL

    return TEST_URL


def _with_engine(work: Any) -> Any:
    async def main() -> Any:
        engine = create_async_engine(postgres_url_value())
        try:
            return await work(factory_for(async_sessionmaker(engine, expire_on_commit=False)))
        finally:
            await engine.dispose()

    return asyncio.run(main())


class _View:
    def __init__(self) -> None:
        self.status = BookStatus.SYNCED
        self.sequence = 10

    def execution_snapshot(self, ref: MarketRef) -> MarketSnapshot:
        book = OrderBook(
            ref=ref,
            bids=tuple(BookLevel(Decimal(100 - i), Decimal(1)) for i in range(1, 4)),
            asks=tuple(BookLevel(Decimal(100 + i), Decimal(1)) for i in range(1, 4)),
            local_timestamp=START + timedelta(milliseconds=self.sequence),
            sequence=self.sequence,
            bids_complete=False,
            asks_complete=False,
        )
        quote = Quote(ref, Decimal(99), Decimal(101), Decimal(1), Decimal(1), START)
        synced = self.status is BookStatus.SYNCED
        return MarketSnapshot(
            ref=ref,
            status=FeedStatus.LIVE,
            quote=quote,
            book=book if synced else None,
            book_status=self.status,
            last_price=None,
            volume_24h=None,
            quote_volume_24h=None,
            latency_ms=None,
            last_update_at=START,
            age_ms=0,
            quote_age_ms=0,
            book_age_ms=0 if synced else None,
            updates=1,
            gaps=0,
            resyncs=1,
        )


@pytest.mark.usefixtures("replay_data")
def test_capture_records_synchronised_depth_once_per_update_and_funding_once() -> None:
    ids = seed(DatasetBuilder())
    view = _View()
    funding = FundingInfo(PERP, Decimal(1), Decimal(1), Decimal("0.0001"), START, START, 8)

    async def work(factory: Any) -> tuple[int, int]:
        recorder = ReplayCaptureRecorder(ids, factory, CaptureConfig(enabled=True, depth_levels=2))
        await recorder.flush(view, PAIR, {PERP: funding})
        await recorder.flush(view, PAIR, {PERP: funding})  # nothing moved
        view.status = BookStatus.SYNCING
        view.sequence = 11
        await recorder.flush(view, PAIR, {PERP: funding})  # unsynced: not recorded
        view.status = BookStatus.SYNCED
        await recorder.flush(view, PAIR, {PERP: funding})
        async with factory() as session:
            books = (await session.execute(select(OrderBookSnapshot))).scalars().all()
            fundings = await session.scalar(select(func.count(FundingObservation.id)))
        assert all(len(book.bids) == 2 and not book.bids_complete for book in books)
        return len(books), int(fundings or 0)

    assert _with_engine(work) == (4, 1)


@pytest.fixture
async def markets(db: AsyncSession) -> dict[MarketRef, int]:
    spot = make_market("BTCUSDT", MarketType.SPOT)
    perp = make_market("BTCUSDT", MarketType.PERPETUAL)
    db.add_all([spot, perp])
    await db.flush()
    return {PNL_SPOT: spot.id, PNL_PERP: perp.id}


class TestSnapshots:
    async def test_pnl_rows_beyond_one_statement_are_written(self, db: AsyncSession) -> None:
        """The first snapshot after a start emits a row per closed position."""
        snapshots = writer(db)
        window_end = datetime(2026, 9, 11, 12, tzinfo=UTC)
        from trading_bot.portfolio.snapshots import Window

        rows = [
            snapshots.pnl_row(
                window=Window("all", None, window_end),
                captured_at=window_end,
                trades=(),
                curve=(),
            )
            | {"scope_key": f"position:{index}"}
            for index in range(1_200)
        ]
        assert await snapshots.write_pnl(rows) == 1_200
        assert await db.scalar(select(func.count(PnlSnapshot.id))) == 1_200

    async def test_folded_snapshots_write_what_reading_history_writes(
        self, db: AsyncSession, markets: dict[MarketRef, int]
    ) -> None:
        rng = random.Random(11)  # noqa: S311 - reproducible test data, not security
        base = datetime(2026, 9, 11, 23, 52, tzinfo=UTC)  # crosses UTC midnight
        store = PortfolioStore(session_factory(db), mode=ExecutionMode.PAPER)
        config = PortfolioConfig(enabled=True, snapshot_interval_ms=60_000)
        clock = [base]
        services = [
            PortfolioService(
                store=store,
                writer=writer(db),
                marks=None,  # type: ignore[arg-type]  (nothing is ever left open)
                closer=None,
                pnl_source=None,
                config=config,
                venue=VENUE,
                clock=lambda: clock[0],
                incremental=incremental,
            )
            for incremental in (False, True)
        ]
        captured: dict[bool, list[list[dict[str, Any]]]] = {False: [], True: []}
        for service, incremental in zip(services, (False, True), strict=True):
            original = service._writer.write_pnl

            async def record(
                rows: Any, incremental: bool = incremental, original: Any = original
            ) -> int:
                captured[incremental].append([dict(row) for row in rows])
                return int(await original(rows))

            service._writer.write_pnl = record  # type: ignore[method-assign]
        for step in range(14):
            closed_at = base + step * INTERVAL + timedelta(seconds=10)
            for index in range(rng.randint(0, 2)):
                await complete_attempt(
                    db,
                    markets,
                    attempt_id=f"t{step}-{index}",
                    buy_exit=str(100_000 + rng.randint(-80, 80)),
                    sell_exit=str(100_100 + rng.randint(-80, 80)),
                    closed_at=closed_at,
                    fee="0.5",
                )
            clock[0] = base + step * INTERVAL + timedelta(seconds=30)
            for service in services:
                await service.snapshot()

        assert len(captured[False]) == len(captured[True]) == 14
        for history, folded in zip(captured[False], captured[True], strict=True):
            assert _normalise(_aggregate(history)) == _normalise(_aggregate(folded))
        # Per-position rows: reading history re-emits a leg closed inside the
        # previous interval (an idempotent upsert); folding emits it once.
        # What reaches the table is the same set of rows.
        assert _positions(captured[False]) == _positions(captured[True])
        assert any(_positions(captured[True]))


def _aggregate(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [row for row in rows if not row["scope_key"].startswith("position:")]


def _positions(snapshots: list[list[dict[str, Any]]]) -> dict[tuple[str, Any], Any]:
    return {
        (row["scope_key"], row["captured_at"]): _normalise([row])[0]
        for rows in snapshots
        for row in rows
        if row["scope_key"].startswith("position:")
    }


def _normalise(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for row in sorted(rows, key=lambda item: (item["window"], item["scope_key"])):
        item = dict(row)
        for ratio in ("sharpe_ratio", "sortino_ratio"):
            if item[ratio] is not None:
                item[ratio] = round(item[ratio], 6)
        item["unmeasured_pnl"] = sorted(item["unmeasured_pnl"] or [])
        out.append(item)
    return out


class TestLiveQueriesIgnoreBacktests:
    @pytest.mark.usefixtures("replay_data")
    async def test_strategy_health_does_not_count_replayed_opportunities(
        self, postgres_url: str
    ) -> None:
        engine = create_async_engine(postgres_url)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with sessions() as session:
                market = make_market("BTCUSDT", venue="replaytest")
                run = BacktestRun(
                    run_uid=uuid.uuid4(),
                    status=BacktestRunStatus.PENDING,
                    dataset_source="postgres",
                    requested_start=START,
                    requested_end=START + timedelta(hours=1),
                    markets=[],
                    config_snapshot={},
                    config_hash="0" * 64,
                )
                session.add_all([market, run])
                await session.flush()
                session.add(
                    make_opportunity(
                        market,
                        detected_at=datetime.now(UTC),
                        mode=ExecutionMode.BACKTEST,
                        backtest_run_id=run.id,
                    )
                )
                await session.commit()
            init_engine(DatabaseConfig(url_override=postgres_url))
            try:
                total, _, _ = await recent_opportunities(datetime.now(UTC) - timedelta(minutes=5))
            finally:
                await dispose_engine()
            assert total == 0
        finally:
            await engine.dispose()

    async def test_recost_reports_on_the_live_record_only(self, db: AsyncSession) -> None:
        market = make_market()
        db.add(market)
        run = BacktestRun(
            run_uid=uuid.uuid4(),
            dataset_source="postgres",
            requested_start=START,
            requested_end=START + timedelta(hours=1),
            markets=[],
            config_snapshot={},
            config_hash="0" * 64,
        )
        db.add(run)
        await db.flush()
        db.add(make_opportunity(market, status=OpportunityStatus.UNPRICEABLE, net_edge_bps=None))
        db.add(
            make_opportunity(
                market,
                status=OpportunityStatus.UNPRICEABLE,
                net_edge_bps=None,
                mode=ExecutionMode.BACKTEST,
                backtest_run_id=run.id,
            )
        )
        await db.flush()
        from trading_bot.core.config import CostsConfig

        report = await recost(db, FeeSchedule.from_config(CostsConfig()))
        assert report.total + report.skipped_unpriceable == 1


def test_reading_committed_rows_in_the_right_scope(replay_data: None) -> None:
    """Sanity for the helpers above: seeded markets are visible to a new engine."""
    ids = seed(DatasetBuilder())

    async def count(session: AsyncSession) -> int:
        from trading_bot.db.models import Market

        return int(
            await session.scalar(select(func.count(Market.id)).where(Market.id.in_(ids.values())))
            or 0
        )

    assert run_async(count) == 2
