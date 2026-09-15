"""Replay capture: one coherent sample per interval, and a lost sample is a recorded gap."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from tests.integration.backtest_support import (
    PAIR,
    PERP,
    SPOT,
    START,
    DatasetBuilder,
    backtest_settings,
    basis_dataset,
    cleanup,
    factory_for,
    seed,
)
from tests.integration.conftest import TEST_URL
from trading_bot.backtest.capture_status import capture_status, render_capture
from trading_bot.backtest.events import EventKind
from trading_bot.backtest.postgres_source import PostgresHistoricalSource
from trading_bot.backtest.service import run_backtest
from trading_bot.backtest.source import DatasetRequest
from trading_bot.core.config import CaptureConfig, Settings
from trading_bot.db.models import (
    FundingObservation,
    Market,
    MarketData,
    OrderBookSnapshot,
    SystemEvent,
)
from trading_bot.db.models.enums import BacktestRunStatus, SystemEventType
from trading_bot.exchange.models import BookLevel, FundingInfo, MarketRef, OrderBook, Quote
from trading_bot.marketdata.capture import CAPTURE_COMPONENT, ReplayCaptureRecorder
from trading_bot.marketdata.models import BookStatus, FeedStatus, MarketSnapshot
from trading_bot.marketdata.recorder import MarketDataRecorder

pytestmark = pytest.mark.requires_postgres


@pytest.fixture(autouse=True)
def _clean(postgres_url: str) -> Iterator[None]:
    cleanup()
    yield
    cleanup()


class Feed:
    """An engine view whose quote, book and clock the test moves."""

    def __init__(self) -> None:
        self.at = START
        self.sequence = 10
        self._quotes: dict[MarketRef, Quote] = {}

    def advance(self, *, seconds: float = 1.0) -> None:
        self.at += timedelta(seconds=seconds)
        self.sequence += 1
        self._quotes.clear()

    def execution_snapshot(self, ref: MarketRef) -> MarketSnapshot:
        quote = self._quotes.setdefault(
            ref, Quote(ref, Decimal(99), Decimal(101), Decimal(1), Decimal(1), self.at)
        )
        book = OrderBook(
            ref=ref,
            bids=(BookLevel(Decimal(99), Decimal(1)),),
            asks=(BookLevel(Decimal(101), Decimal(1)),),
            local_timestamp=self.at,
            sequence=self.sequence,
        )
        return MarketSnapshot(
            ref=ref,
            status=FeedStatus.LIVE,
            quote=quote,
            book=book,
            book_status=BookStatus.SYNCED,
            last_price=None,
            volume_24h=None,
            quote_volume_24h=None,
            latency_ms=None,
            last_update_at=self.at,
            age_ms=0,
            quote_age_ms=0,
            book_age_ms=0,
            updates=1,
            gaps=0,
            resyncs=0,
        )

    def funding(self) -> dict[MarketRef, FundingInfo]:
        return {
            PERP: FundingInfo(
                PERP,
                Decimal(100),
                Decimal(100),
                Decimal("0.0001"),
                START + timedelta(hours=1),
                self.at,
                8,
            )
        }


class BrokenFeed(Feed):
    def __init__(self) -> None:
        super().__init__()
        self.broken = True

    def execution_snapshot(self, ref: MarketRef) -> MarketSnapshot:
        if self.broken and ref == PERP:
            raise RuntimeError("perpetual book unavailable")
        return super().execution_snapshot(ref)


async def with_database(work: Any) -> Any:
    engine = create_async_engine(TEST_URL)
    try:
        return await work(engine, factory_for(async_sessionmaker(engine, expire_on_commit=False)))
    finally:
        await engine.dispose()


def failing_once(factory: Any, failures: dict[str, int]) -> Any:
    @asynccontextmanager
    async def scope() -> AsyncIterator[AsyncSession]:
        async with factory() as session:
            yield session
            if failures["left"]:
                failures["left"] -= 1
                raise ConnectionError("database restarting")

    return scope


async def counts(factory: Any) -> tuple[int, int, int, int]:
    async with factory() as session:
        return (
            int(await session.scalar(select(func.count(MarketData.id))) or 0),
            int(await session.scalar(select(func.count(OrderBookSnapshot.id))) or 0),
            int(await session.scalar(select(func.count(FundingObservation.id))) or 0),
            int(
                await session.scalar(
                    select(func.count(SystemEvent.id)).where(
                        SystemEvent.component == CAPTURE_COMPONENT
                    )
                )
                or 0
            ),
        )


class TestSamples:
    def test_a_sample_writes_quotes_books_and_funding_together_once_each(self) -> None:
        ids = seed(DatasetBuilder())
        feed = Feed()

        async def work(_: AsyncEngine, factory: Any) -> tuple[Any, ...]:
            recorder = ReplayCaptureRecorder(ids, factory, CaptureConfig(enabled=True))
            await recorder.flush(feed, PAIR, feed.funding())
            await recorder.flush(feed, PAIR, feed.funding())  # nothing new
            return await counts(factory), recorder.health.samples_written

        assert asyncio.run(with_database(work)) == ((2, 2, 1, 0), 1)

    def test_a_failed_sample_is_never_retried_and_becomes_a_recorded_gap(self) -> None:
        ids = seed(DatasetBuilder())
        feed = Feed()
        failures = {"left": 1}

        async def work(_: AsyncEngine, factory: Any) -> tuple[Any, ...]:
            recorder = ReplayCaptureRecorder(
                ids,
                failing_once(factory, failures),
                CaptureConfig(enabled=True),
                clock=lambda: feed.at,
            )
            await recorder.flush(feed, PAIR, feed.funding())  # lost whole
            lost = await counts(factory)
            feed.advance(seconds=3)
            await recorder.flush(feed, PAIR, feed.funding())
            async with factory() as session:
                sequences = sorted(
                    (await session.execute(select(OrderBookSnapshot.sequence))).scalars()
                )
                gap = (
                    await session.execute(
                        select(SystemEvent).where(SystemEvent.component == CAPTURE_COMPONENT)
                    )
                ).scalar_one()
            return lost, sequences, gap, recorder.health

        lost, sequences, gap, health = asyncio.run(with_database(work))
        assert lost == (0, 0, 0, 0), "a sample lands whole or not at all"
        assert sequences == [11, 11], "the book written is the current one, not the lost one"
        assert gap.event_type is SystemEventType.DATA_GAP
        assert gap.context["gap_start"] == START.isoformat()
        assert gap.context["gap_end"] == (START + timedelta(seconds=3)).isoformat()
        assert gap.context["samples_lost"] == 1
        assert (health.failures, health.samples_lost, health.gaps_recorded) == (1, 1, 1)

    def test_a_market_read_failure_loses_the_coherent_sample_and_names_the_market(self) -> None:
        ids = seed(DatasetBuilder())
        feed = BrokenFeed()

        async def work(_: AsyncEngine, factory: Any) -> tuple[Any, ...]:
            recorder = ReplayCaptureRecorder(
                ids, factory, CaptureConfig(enabled=True), clock=lambda: feed.at
            )
            await recorder.flush(feed, PAIR, feed.funding())
            lost = await counts(factory)
            feed.broken = False
            feed.advance(seconds=1)
            await recorder.flush(feed, PAIR, feed.funding())
            async with factory() as session:
                gap = (
                    await session.execute(
                        select(SystemEvent).where(SystemEvent.component == CAPTURE_COMPONENT)
                    )
                ).scalar_one()
            return lost, gap.context, recorder.health

        lost, context, health = asyncio.run(with_database(work))
        assert lost == (0, 0, 0, 0)
        assert context["markets"] == [str(PERP)]
        assert "perpetual book unavailable" in context["error"]
        assert (health.failures, health.samples_lost, health.gaps_recorded) == (1, 1, 1)

    def test_a_gap_open_at_shutdown_is_recorded_on_close(self) -> None:
        ids = seed(DatasetBuilder())
        feed = Feed()
        failures = {"left": 1}

        async def work(_: AsyncEngine, factory: Any) -> tuple[int, int, int, int]:
            recorder = ReplayCaptureRecorder(
                ids, failing_once(factory, failures), CaptureConfig(enabled=True)
            )
            await recorder.flush(feed, PAIR, feed.funding())
            await recorder.close()
            return await counts(factory)

        assert asyncio.run(with_database(work)) == (0, 0, 0, 1)

    def test_while_capture_runs_the_quote_recorder_writes_events_but_no_quotes(self) -> None:
        ids = seed(DatasetBuilder())
        feed = Feed()

        async def work(_: AsyncEngine, factory: Any) -> int:
            recorder = MarketDataRecorder(ids, factory, interval_seconds=1, quotes=False)
            await recorder.flush([feed.execution_snapshot(SPOT)])
            return recorder.rows_written

        assert asyncio.run(with_database(work)) == 0

    def test_captured_rows_replay_as_the_state_that_was_captured(self) -> None:
        ids = seed(DatasetBuilder())
        feed = Feed()
        feed.at = START + timedelta(minutes=10)

        async def work(engine: AsyncEngine, factory: Any) -> list[Any]:
            recorder = ReplayCaptureRecorder(ids, factory, CaptureConfig(enabled=True))
            await recorder.flush(feed, PAIR, feed.funding())
            source = PostgresHistoricalSource(engine)
            request = DatasetRequest(feed.at, feed.at + timedelta(seconds=1), PAIR)
            await source.open()
            try:
                await source.coverage(request)
                return [event async for batch in source.stream(request) for event in batch.events]
            finally:
                await source.close()

        events = asyncio.run(with_database(work))
        kinds = sorted((str(event.ref), event.kind) for event in events)
        assert kinds == sorted(
            [
                (str(SPOT), EventKind.QUOTE),
                (str(SPOT), EventKind.BOOK),
                (str(PERP), EventKind.QUOTE),
                (str(PERP), EventKind.BOOK),
                (str(PERP), EventKind.FUNDING),
            ]
        )
        assert {event.available_at for event in events} == {feed.at}
        spot_book = next(e for e in events if e.ref == SPOT and e.kind is EventKind.BOOK)
        assert spot_book.payload == feed.execution_snapshot(SPOT).book


class TestCadence:
    def test_a_capture_cadence_replay_would_read_as_stale_is_refused(self) -> None:
        with pytest.raises(ValueError, match="more than half"):
            Settings(market_data={"capture": {"enabled": True, "interval_ms": 1_500}})
        with pytest.raises(ValueError, match="never be attributed"):
            Settings(
                market_data={"capture": {"enabled": True}},
                strategy={"spot_perp_basis": {"funding_refresh_seconds": 300}},
            )
        assert Settings(market_data={"capture": {"enabled": True}}).market_data.capture.enabled
        # Off by default, and unconstrained while off.
        assert not Settings(
            market_data={"capture": {"interval_ms": 5_000}}
        ).market_data.capture.enabled


class TestGapsReachReplayAndStatus:
    def _gap(self, factory: Any, start_offset: int, end_offset: int) -> None:
        async def write(session: AsyncSession) -> None:
            session.add(
                SystemEvent(
                    occurred_at=START + timedelta(seconds=start_offset),
                    event_type=SystemEventType.DATA_GAP,
                    component=CAPTURE_COMPONENT,
                    message="replay capture wrote nothing",
                    context={
                        "gap_start": (START + timedelta(seconds=start_offset)).isoformat(),
                        "gap_end": (START + timedelta(seconds=end_offset)).isoformat(),
                    },
                )
            )

        from tests.integration.backtest_support import run_async

        run_async(write)

    def test_a_recorded_gap_inside_the_range_makes_the_replay_incomplete(self) -> None:
        seed(basis_dataset(0, seconds=40))
        self._gap(None, 10, 15)
        outcome = run_backtest(
            backtest_settings(), start=START, end=START + timedelta(seconds=30), refs=PAIR
        )
        assert outcome.status is BacktestRunStatus.INCOMPLETE
        assert outcome.completeness["dataset"]["issues"] == {"capture_gap": 1}

    def test_a_gap_outside_the_range_does_not(self) -> None:
        seed(basis_dataset(0, seconds=40))
        self._gap(None, -120, -60)
        outcome = run_backtest(
            backtest_settings(), start=START, end=START + timedelta(seconds=30), refs=PAIR
        )
        assert outcome.status is BacktestRunStatus.COMPLETED, outcome.completeness

    def test_capture_status_reads_rows_and_gaps_from_the_database(self) -> None:
        seed(basis_dataset(0, seconds=40))
        self._gap(None, 10, 15)

        async def work(_: AsyncEngine, factory: Any) -> Any:
            async with factory() as session:
                markets = (await session.execute(select(Market))).scalars().all()
                for market in markets:
                    market.is_monitored = True
            return await capture_status(
                factory,
                venue="replaytest",
                until=START + timedelta(minutes=1),
                window=timedelta(minutes=2),
            )

        status = asyncio.run(with_database(work))
        spot = next(row for row in status.markets if row.market.endswith("SPOT"))
        assert spot.quotes.rows == 40 and spot.books.rows == 40 and spot.funding is None
        assert len(status.gaps) == 1
        assert "recorded gaps: 1" in render_capture(status)
