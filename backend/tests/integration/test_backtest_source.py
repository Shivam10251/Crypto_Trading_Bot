"""The PostgreSQL history source: order, bounded memory, one immutable view, initial state."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterator
from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import delete, insert
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from tests.integration.backtest_support import (
    PAIR,
    PERP,
    SPOT,
    START,
    DatasetBuilder,
    cleanup,
    seed,
)
from tests.integration.conftest import TEST_URL
from trading_bot.backtest.events import EventKind, ReplayEvent
from trading_bot.backtest.postgres_source import PostgresHistoricalSource, _Cursor
from trading_bot.backtest.source import DatasetRequest, EventValidator, Lookback
from trading_bot.db.models import MarketData, OrderBookSnapshot

pytestmark = pytest.mark.requires_postgres

WINDOW = DatasetRequest(START, START + timedelta(minutes=1), PAIR, batch_size=7)
LOOKBACK = Lookback(
    quote=timedelta(seconds=30), book=timedelta(seconds=30), funding=timedelta(minutes=10)
)


@pytest.fixture(autouse=True)
def replay_data(postgres_url: str) -> Iterator[None]:
    cleanup()
    yield
    cleanup()


def with_engine[T](work: Callable[[AsyncEngine], Awaitable[T]]) -> T:
    async def main() -> T:
        engine = create_async_engine(TEST_URL)
        try:
            return await work(engine)
        finally:
            await engine.dispose()

    return asyncio.run(main())


async def read_all(source: PostgresHistoricalSource, request: DatasetRequest) -> list[ReplayEvent]:
    return [event async for batch in source.stream(request) for event in batch.events]


def fingerprint(events: list[ReplayEvent], request: DatasetRequest) -> str:
    validator = EventValidator(request, gap_ms=10_000)
    for event in events:
        validator.accept(event)
    return validator.fingerprint.hexdigest()


def steady(seconds: int, *, start_offset: float = 0.0, funding_every: int = 30) -> DatasetBuilder:
    dataset = DatasetBuilder()
    for step in range(seconds):
        at = START + timedelta(seconds=start_offset + step)
        dataset.market_pair(at, spot_mid=Decimal(100_000), basis_bps=Decimal(step % 5))
        if step % funding_every == 0:
            dataset.observe_funding(PERP, at)
    return dataset


class TestStreaming:
    def test_streams_in_bounded_batches_in_a_total_deterministic_order(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        dataset = DatasetBuilder()
        for step in range(40):
            # Every stream shares each receipt instant: the tie-break decides.
            at = START + timedelta(milliseconds=250 * (step // 2))
            dataset.market_pair(at, spot_mid=Decimal(100_000), basis_bps=Decimal(step % 5))
            if step % 2 == 0:  # one observation per receipt instant, as the table requires
                dataset.observe_funding(PERP, at)
        seed(dataset)
        largest_page: list[int] = []
        original = PostgresHistoricalSource._fill

        async def spy(self: Any, cursor: _Cursor, *args: Any) -> None:
            await original(self, cursor, *args)
            largest_page.append(len(cursor.buffer))

        monkeypatch.setattr(PostgresHistoricalSource, "_fill", spy)

        async def stream(engine: AsyncEngine) -> list[list[ReplayEvent]]:
            source = PostgresHistoricalSource(engine)
            await source.open()
            try:
                await source.coverage(WINDOW)
                return [batch.events async for batch in source.stream(WINDOW)]
            finally:
                await source.close()

        batches = with_engine(stream)
        events = [event for batch in batches for event in batch]
        assert all(len(batch) <= 7 for batch in batches)
        assert max(largest_page) <= 7, "no page larger than a batch is ever held"
        assert len(events) == len(dataset.quotes) + len(dataset.books) + len(dataset.funding)
        keys = [event.order_key for event in events]
        assert keys == sorted(keys) and len(set(keys)) == len(keys)
        first_instant = [event.kind for event in events if event.available_at == START]
        assert first_instant == sorted(first_instant), "funding, then books, then quotes"
        assert first_instant[0] is EventKind.FUNDING

    def test_coverage_counts_each_stream_and_names_unknown_markets(self) -> None:
        dataset = DatasetBuilder()
        dataset.market_pair(START, spot_mid=Decimal(100_000), basis_bps=Decimal(1))
        seed(dataset)
        missing = SPOT.__class__("replaytest", "NOPEUSDT", SPOT.market_type)

        async def cover(engine: AsyncEngine) -> Any:
            source = PostgresHistoricalSource(engine)
            await source.open()
            try:
                return await source.coverage(
                    DatasetRequest(START, START + timedelta(seconds=1), (*PAIR, missing))
                )
            finally:
                await source.close()

        coverage = with_engine(cover)
        assert coverage.unknown == (missing,)
        assert {(m.ref, m.quotes, m.books, m.funding) for m in coverage.markets} == {
            (SPOT, 1, 1, 0),
            (PERP, 1, 1, 0),
        }
        assert coverage.specs[PERP].market_notional_uses_mark_price
        assert coverage.unsupported_filters
        assert set(coverage.spec_versions) == set(PAIR)
        assert (coverage.consistency or "").startswith("postgres repeatable-read snapshot")

    def test_a_closed_source_refuses_to_read(self) -> None:
        async def unopened(engine: AsyncEngine) -> None:
            with pytest.raises(RuntimeError, match="not open"):
                await PostgresHistoricalSource(engine).coverage(WINDOW)

        with_engine(unopened)


class TestImmutableView:
    def test_rows_inserted_or_deleted_during_a_stream_are_invisible_to_it(self) -> None:
        ids = seed(steady(60))

        async def mutate_mid_stream(engine: AsyncEngine) -> tuple[list[ReplayEvent], int]:
            source = PostgresHistoricalSource(engine)
            await source.open()
            try:
                await source.coverage(WINDOW)
                events: list[ReplayEvent] = []
                mutated = False
                async for batch in source.stream(WINDOW):
                    events.extend(batch.events)
                    if not mutated:
                        mutated = True
                        async with engine.begin() as writer:
                            # A late capture write inside the window, ahead of the
                            # cursor, and a retention purge of rows not yet read.
                            late = await writer.execute(
                                insert(MarketData).values(
                                    market_id=ids[SPOT],
                                    bid=Decimal(1),
                                    ask=Decimal(2),
                                    bid_size=Decimal(1),
                                    ask_size=Decimal(1),
                                    mid_price=Decimal("1.5"),
                                    spread=Decimal(1),
                                    spread_bps=Decimal(1),
                                    local_timestamp=START + timedelta(seconds=45, milliseconds=5),
                                )
                            )
                            assert late.rowcount == 1
                            purged = await writer.execute(
                                delete(OrderBookSnapshot).where(
                                    OrderBookSnapshot.local_timestamp
                                    >= START + timedelta(seconds=30)
                                )
                            )
                return events, purged.rowcount
            finally:
                await source.close()

        async def fresh_read(engine: AsyncEngine) -> list[ReplayEvent]:
            source = PostgresHistoricalSource(engine)
            await source.open()
            try:
                await source.coverage(WINDOW)
                return await read_all(source, WINDOW)
            finally:
                await source.close()

        events, purged = with_engine(mutate_mid_stream)
        assert purged > 0
        # Exactly the dataset as it stood when the view opened.
        dataset = steady(60)
        assert len(events) == len(dataset.quotes) + len(dataset.books) + len(dataset.funding)
        assert not any(
            event.payload.bid == Decimal(1) for event in events if hasattr(event.payload, "bid")
        )
        # A run opened afterwards sees the new state, and says so in its fingerprint.
        after = with_engine(fresh_read)
        assert len(after) == len(events) + 1 - purged
        assert fingerprint(events, WINDOW) != fingerprint(after, WINDOW)

    def test_the_same_source_state_gives_the_same_inputs_whatever_the_page_size(self) -> None:
        seed(steady(60))

        async def read(engine: AsyncEngine, batch_size: int) -> list[ReplayEvent]:
            source = PostgresHistoricalSource(engine)
            request = DatasetRequest(WINDOW.start, WINDOW.end, PAIR, batch_size=batch_size)
            await source.open()
            try:
                await source.coverage(request)
                return await read_all(source, request)
            finally:
                await source.close()

        small = with_engine(lambda engine: read(engine, 3))
        large = with_engine(lambda engine: read(engine, 500))
        assert [event.order_key for event in small] == [event.order_key for event in large]
        assert fingerprint(small, WINDOW) == fingerprint(large, WINDOW)


class TestInitialState:
    def test_the_newest_valid_rows_before_the_start_are_offered_newest_first(self) -> None:
        dataset = DatasetBuilder()
        for step in range(5):  # a pair every 2 s, ending 1 s before the start
            at = START - timedelta(seconds=9 - 2 * step)
            dataset.market_pair(at, spot_mid=Decimal(100_000), basis_bps=Decimal(step))
        dataset.observe_funding(PERP, START - timedelta(seconds=25))
        dataset.observe_funding(PERP, START - timedelta(minutes=20))  # beyond the carry limit
        # Too old for a book or a quote.
        dataset.market_pair(
            START - timedelta(seconds=40), spot_mid=Decimal(1), basis_bps=Decimal(0)
        )
        dataset.market_pair(START, spot_mid=Decimal(100_000), basis_bps=Decimal(0))
        seed(dataset)

        async def initial(engine: AsyncEngine) -> Any:
            source = PostgresHistoricalSource(engine)
            await source.open()
            try:
                await source.coverage(WINDOW)
                return await source.initial_state(WINDOW, LOOKBACK)
            finally:
                await source.close()

        batch = with_engine(initial)
        assert batch.corrupt == []
        assert all(event.available_at < START for event in batch.events)
        assert all(
            event.available_at >= START - LOOKBACK.for_kind(event.kind) for event in batch.events
        )
        validator = EventValidator(WINDOW, gap_ms=10_000)
        chosen = validator.initialize(batch.events)
        assert {(event.ref, event.kind) for event in chosen} == {
            (SPOT, EventKind.QUOTE),
            (SPOT, EventKind.BOOK),
            (PERP, EventKind.QUOTE),
            (PERP, EventKind.BOOK),
            (PERP, EventKind.FUNDING),
        }
        books = {event.ref: event for event in chosen if event.kind is EventKind.BOOK}
        assert {event.available_at for event in books.values()} == {START - timedelta(seconds=1)}
        (funding,) = [event for event in chosen if event.kind is EventKind.FUNDING]
        assert funding.available_at == START - timedelta(seconds=25)
        assert validator.initialization_events == 5
        assert validator.events_accepted == 0

    def test_more_than_ten_invalid_newest_rows_do_not_hide_an_older_valid_state(self) -> None:
        dataset = DatasetBuilder()
        dataset.quote(SPOT, START - timedelta(seconds=20), "99", "101")
        for seconds in range(12, 1, -1):
            dataset.quote(
                SPOT,
                START - timedelta(seconds=seconds),
                "99",
                "101",
                exchange_timestamp=START + timedelta(days=1),
            )
        seed(dataset)

        async def initial(engine: AsyncEngine) -> Any:
            source = PostgresHistoricalSource(engine)
            await source.open()
            try:
                await source.coverage(WINDOW)
                return await source.initial_state(WINDOW, LOOKBACK)
            finally:
                await source.close()

        batch = with_engine(initial)
        assert batch.corrupt == []
        validator = EventValidator(WINDOW, gap_ms=10_000)
        chosen = validator.initialize(batch.events)
        (quote,) = chosen
        assert quote.available_at == START - timedelta(seconds=20)
