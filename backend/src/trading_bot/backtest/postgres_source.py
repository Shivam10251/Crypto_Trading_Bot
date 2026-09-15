"""Replaying the repository's own durable market data.

Three tables, merged into one stream ordered by ``(local_timestamp, kind,
id)``:

| Table | Becomes | Recorded by |
| --- | --- | --- |
| ``market_data`` | ``Quote`` + 24h volume | market-data service (capture sets its cadence) |
| ``order_books`` | ``OrderBook``, sequenced | Phase 11 capture, off by default |
| ``funding_observations`` | ``FundingInfo`` | Phase 11 capture, off by default |

**One immutable view per run.** ``open`` starts a single ``REPEATABLE READ
READ ONLY`` transaction on a dedicated connection, and coverage,
initialization and every page are read inside it. PostgreSQL gives every
statement of that transaction the snapshot taken by its first statement, so
rows the capture writes while the replay runs are invisible, and rows the
retention job deletes stay readable until ``close``. No lock is taken: writers
are never blocked. The costs are the ones any long snapshot has - the vacuum
horizon is held back for the run's duration, so dead rows in the history
tables cannot be reclaimed until it ends, and a server with a short
``idle_in_transaction_session_timeout`` or ``statement_timeout`` can end a
long run, which then fails rather than reading a different state. Each query
is counted as replay I/O individually; the open transaction between queries is
not, or virtual time could never advance.

**Bounded memory.** Each table is read by keyset pagination on
``(local_timestamp, id)`` - never ``OFFSET``, never the whole range - and at
most one page per table is held while merging. A batch never exceeds
``batch_size`` events.

**What the tables cannot say.** ``markets`` holds the filters the service
read at its *latest* start, not the ones in force when the data was recorded,
and it never stored the price bounds, percent-price bands, maximum notional or
the spot average-price notional reference. Those checks are skipped by the
simulator for want of inputs - reported in ``unsupported_filters``, never
silently assumed to pass.
"""

from __future__ import annotations

import json
from collections import deque
from collections.abc import AsyncIterator, Sequence
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import DateTime, Select, and_, func, literal, or_, select, text, tuple_
from sqlalchemy.engine import Result
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncTransaction

from trading_bot.backtest.events import EventKind, ReplayEvent
from trading_bot.backtest.loop import replay_io
from trading_bot.backtest.source import (
    DatasetCoverage,
    DatasetRequest,
    Lookback,
    MarketCoverage,
    ReplayBatch,
)
from trading_bot.db.models import (
    FundingObservation,
    Market,
    MarketData,
    OrderBookSnapshot,
    SystemEvent,
)
from trading_bot.db.models.enums import MarketType, SystemEventType
from trading_bot.exchange.errors import ExchangeDataError
from trading_bot.exchange.models import (
    BookLevel,
    FundingInfo,
    MarketRef,
    MarketSpec,
    OrderBook,
    Quote,
)
from trading_bot.marketdata.capture import CAPTURE_COMPONENT

#: Venue filters the ``markets`` table does not persist.
UNSUPPORTED_FILTERS = (
    "min_price/max_price",
    "percent_price bands",
    "max_notional",
    "spot average-price notional reference",
    "filters as they were when the data was recorded (only the latest are stored)",
)
_TABLES: dict[EventKind, Any] = {
    EventKind.QUOTE: MarketData.__table__,
    EventKind.BOOK: OrderBookSnapshot.__table__,
    EventKind.FUNDING: FundingObservation.__table__,
}


def spec_from_row(row: Any) -> MarketSpec:
    """The reference data an order has to satisfy, as far as it was stored."""
    return MarketSpec(
        ref=MarketRef(row.venue, row.symbol, row.market_type),
        base_asset=row.base_asset,
        quote_asset=row.quote_asset,
        is_active=row.is_active,
        tick_size=row.tick_size,
        step_size=row.step_size,
        min_notional=row.min_notional,
        min_qty=row.min_qty,
        max_qty=row.max_qty,
        market_min_qty=row.market_min_qty,
        market_max_qty=row.market_max_qty,
        market_step_size=row.market_step_size,
        maker_fee_bps=row.maker_fee_bps,
        taker_fee_bps=row.taker_fee_bps,
        contract_size=row.contract_size,
        settlement_asset=row.settlement_asset,
        # The venue rule the live mapping applies: USD-M validates a MARKET
        # order's notional against mark price. Replay supplies the recorded
        # mark, so the check stays as strict as it is live.
        market_notional_uses_mark_price=row.market_type is MarketType.PERPETUAL,
    )


class PostgresHistoricalSource:
    name = "postgres"

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        self._connection: AsyncConnection | None = None
        self._transaction: AsyncTransaction | None = None
        self._market_ids: dict[MarketRef, int] = {}
        self._refs_by_id: dict[int, MarketRef] = {}
        self._specs: dict[MarketRef, MarketSpec] = {}
        self._versions: dict[MarketRef, str] = {}
        self.snapshot_id: str | None = None

    @property
    def market_ids(self) -> dict[MarketRef, int]:
        """``markets.id`` per ref, once ``coverage`` loaded them."""
        return dict(self._market_ids)

    async def open(self) -> None:
        if self._connection is not None:
            return
        async with replay_io():
            connection = await self._engine.connect()
            try:
                await connection.execution_options(
                    isolation_level="REPEATABLE READ", postgresql_readonly=True
                )
                self._transaction = await connection.begin()
                # The first statement fixes the snapshot every later one reads.
                self.snapshot_id = str(
                    await connection.scalar(text("SELECT pg_current_snapshot()::text"))
                )
            except BaseException:
                await connection.close()
                raise
        self._connection = connection

    async def close(self) -> None:
        connection, self._connection = self._connection, None
        transaction, self._transaction = self._transaction, None
        if connection is None:
            return
        async with replay_io():
            try:
                if transaction is not None and transaction.is_active:
                    await transaction.rollback()
            finally:
                await connection.close()

    async def coverage(self, request: DatasetRequest) -> DatasetCoverage:
        await self._load_markets(request.refs)
        unknown = tuple(ref for ref in request.refs if ref not in self._market_ids)
        markets: list[MarketCoverage] = []
        for ref in request.refs:
            market_id = self._market_ids.get(ref)
            if market_id is None:
                continue
            counts: dict[EventKind, tuple[int, datetime | None, datetime | None]] = {}
            for kind, table in _TABLES.items():
                result = await self._execute(
                    select(
                        func.count(table.c.id),
                        func.min(table.c.local_timestamp),
                        func.max(table.c.local_timestamp),
                    ).where(
                        table.c.market_id == market_id,
                        table.c.local_timestamp >= request.start,
                        table.c.local_timestamp < request.end,
                    )
                )
                count, first, last = result.one()
                counts[kind] = (int(count), first, last)
            firsts = [value[1] for value in counts.values() if value[1] is not None]
            lasts = [value[2] for value in counts.values() if value[2] is not None]
            markets.append(
                MarketCoverage(
                    ref=ref,
                    quotes=counts[EventKind.QUOTE][0],
                    books=counts[EventKind.BOOK][0],
                    funding=counts[EventKind.FUNDING][0],
                    first_at=min(firsts) if firsts else None,
                    last_at=max(lasts) if lasts else None,
                )
            )
        return DatasetCoverage(
            capture_gaps=await self._capture_gaps(request),
            markets=tuple(markets),
            unknown=unknown,
            specs={ref: self._specs[ref] for ref in request.refs if ref in self._specs},
            unsupported_filters=UNSUPPORTED_FILTERS,
            spec_versions={ref: self._versions[ref] for ref in request.refs if ref in self._specs},
            consistency=f"postgres repeatable-read snapshot {self.snapshot_id}",
        )

    async def initial_state(self, request: DatasetRequest, lookback: Lookback) -> ReplayBatch:
        batch = ReplayBatch(events=[])
        for ref in request.refs:
            market_id = self._market_ids.get(ref)
            if market_id is None:
                continue
            for kind, table in _TABLES.items():
                if kind is EventKind.FUNDING and ref.market_type is MarketType.SPOT:
                    continue
                result = await self._execute(
                    select(table)
                    .where(
                        table.c.market_id == market_id,
                        table.c.local_timestamp < request.start,
                        table.c.local_timestamp >= request.start - lookback.for_kind(kind),
                    )
                    .order_by(table.c.local_timestamp.desc(), table.c.id.desc())
                )
                for row in result:
                    self._convert(kind, row, batch.events, batch.corrupt)
        return batch

    async def stream(self, request: DatasetRequest) -> AsyncIterator[ReplayBatch]:
        ids = [self._market_ids[ref] for ref in request.refs if ref in self._market_ids]
        if not ids:
            return
        cursors = {kind: _Cursor(kind, table) for kind, table in _TABLES.items()}
        batch = ReplayBatch(events=[])
        while True:
            for cursor in cursors.values():
                if not cursor.buffer and not cursor.exhausted:
                    await self._fill(cursor, request, ids, batch)
            live = [cursor for cursor in cursors.values() if cursor.buffer]
            if not live:
                break
            head = min(live, key=lambda cursor: cursor.buffer[0].order_key)
            batch.events.append(head.buffer.popleft())
            if len(batch.events) >= request.batch_size:
                yield batch
                batch = ReplayBatch(events=[])
        if batch.events or batch.corrupt:
            yield batch

    # --- internals ------------------------------------------------------

    async def _execute(self, statement: Select[Any]) -> Result[Any]:
        if self._connection is None:
            raise RuntimeError("the historical source is not open")
        async with replay_io():
            return await self._connection.execute(statement)

    async def _capture_gaps(self, request: DatasetRequest) -> tuple[str, ...]:
        """``DATA_GAP`` events the capture recorder wrote that overlap the range."""
        events = SystemEvent.__table__
        gap_end = events.c.context["gap_end"].astext.cast(DateTime(timezone=True))
        result = await self._execute(
            select(events.c.id, events.c.occurred_at, events.c.message, events.c.context)
            .where(
                events.c.component == CAPTURE_COMPONENT,
                events.c.event_type == SystemEventType.DATA_GAP,
                events.c.occurred_at < request.end,
                gap_end > request.start,
            )
            .order_by(events.c.occurred_at, events.c.id)
        )
        return tuple(
            "system_events.id="
            f"{row.id} at {row.occurred_at.isoformat()}: {row.message}; "
            f"context={json.dumps(row.context or {}, sort_keys=True, separators=(',', ':'))}"
            for row in result
        )

    async def _load_markets(self, refs: Sequence[MarketRef]) -> None:
        wanted = {(ref.venue, ref.symbol, ref.market_type) for ref in refs}
        markets = Market.__table__
        result = await self._execute(
            select(markets).where(
                or_(
                    *(
                        and_(
                            markets.c.venue == venue,
                            markets.c.symbol == symbol,
                            markets.c.market_type == market_type,
                        )
                        for venue, symbol, market_type in sorted(wanted)
                    )
                )
            )
        )
        for row in result:
            spec = spec_from_row(row)
            self._market_ids[spec.ref] = row.id
            self._refs_by_id[row.id] = spec.ref
            self._specs[spec.ref] = spec
            self._versions[spec.ref] = row.updated_at.isoformat()

    async def _fill(
        self, cursor: _Cursor, request: DatasetRequest, ids: list[int], batch: ReplayBatch
    ) -> None:
        table = cursor.table
        conditions = [
            table.c.market_id.in_(ids),
            table.c.local_timestamp >= request.start,
            table.c.local_timestamp < request.end,
        ]
        if cursor.after is not None:
            after_at, after_id = cursor.after
            conditions.append(
                tuple_(table.c.local_timestamp, table.c.id)
                > tuple_(literal(after_at), literal(after_id))
            )
        fetched = (
            await self._execute(
                select(table)
                .where(*conditions)
                .order_by(table.c.local_timestamp, table.c.id)
                .limit(request.batch_size)
            )
        ).all()
        if len(fetched) < request.batch_size:
            cursor.exhausted = True
        for row in fetched:
            cursor.after = (row.local_timestamp, row.id)
            self._convert(cursor.kind, row, cursor.buffer, batch.corrupt)

    def _convert(
        self,
        kind: EventKind,
        row: Any,
        into: list[ReplayEvent] | deque[ReplayEvent],
        corrupt: list[tuple[str, str]],
    ) -> None:
        ref = self._refs_by_id[row.market_id]
        try:
            into.append(_CONVERTERS[kind](row, ref))
        except (ExchangeDataError, ValueError, TypeError, ArithmeticError) as exc:
            corrupt.append((kind.name, f"{_TABLES[kind].name}.id={row.id}: {exc}"))


class _Cursor:
    __slots__ = ("after", "buffer", "exhausted", "kind", "table")

    def __init__(self, kind: EventKind, table: Any) -> None:
        self.kind = kind
        self.table = table
        self.after: tuple[datetime, int] | None = None
        self.buffer: deque[ReplayEvent] = deque()
        self.exhausted = False


def _quote_event(row: Any, ref: MarketRef) -> ReplayEvent:
    quote = Quote(
        ref=ref,
        bid=row.bid,
        ask=row.ask,
        bid_size=row.bid_size,
        ask_size=row.ask_size,
        local_timestamp=row.local_timestamp,
        exchange_timestamp=row.exchange_timestamp,
        sequence=row.sequence,
    )
    return ReplayEvent(
        kind=EventKind.QUOTE,
        ref=ref,
        available_at=row.local_timestamp,
        source_id=row.id,
        payload=quote,
        sequence=row.sequence,
        volume_24h=row.volume_24h,
    )


def _levels(raw: Any, side: str) -> tuple[BookLevel, ...]:
    if not isinstance(raw, list):
        raise ExchangeDataError(f"{side} is not a list of levels")
    levels: list[BookLevel] = []
    for entry in raw:
        if not isinstance(entry, list | tuple) or len(entry) != 2:
            raise ExchangeDataError(f"malformed {side} level {entry!r}")
        levels.append(BookLevel(Decimal(str(entry[0])), Decimal(str(entry[1]))))
    return tuple(levels)


def _book_event(row: Any, ref: MarketRef) -> ReplayEvent:
    if row.sequence is None:
        # A book the engine cannot sequence was never a synchronised book.
        raise ExchangeDataError("order book snapshot has no sequence")
    book = OrderBook(
        ref=ref,
        bids=_levels(row.bids, "bids"),
        asks=_levels(row.asks, "asks"),
        local_timestamp=row.local_timestamp,
        exchange_timestamp=row.exchange_timestamp,
        sequence=row.sequence,
        bids_complete=row.bids_complete,
        asks_complete=row.asks_complete,
    )
    return ReplayEvent(
        kind=EventKind.BOOK,
        ref=ref,
        available_at=row.local_timestamp,
        source_id=row.id,
        payload=book,
        sequence=row.sequence,
    )


def _funding_event(row: Any, ref: MarketRef) -> ReplayEvent:
    funding = FundingInfo(
        ref=ref,
        mark_price=row.mark_price,
        index_price=row.index_price,
        last_funding_rate=row.funding_rate,
        next_funding_time=row.next_funding_time,
        local_timestamp=row.local_timestamp,
        funding_interval_hours=row.funding_interval_hours,
    )
    return ReplayEvent(
        kind=EventKind.FUNDING,
        ref=ref,
        available_at=row.local_timestamp,
        source_id=row.id,
        payload=funding,
    )


_CONVERTERS = {
    EventKind.QUOTE: _quote_event,
    EventKind.BOOK: _book_event,
    EventKind.FUNDING: _funding_event,
}
