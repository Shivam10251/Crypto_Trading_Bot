"""Recording what a backtest needs to replay: coherent quote, depth and funding samples.

``market_data`` alone cannot be replayed: at its default 5 s cadence every
quote is older than the strategy's 2 s freshness limit for most of the
interval, and it holds no depth or funding. This recorder, off by default
(``market_data.capture.enabled``), takes one **sample** per interval:

- **Quotes**, each market's current top of book if it changed. While capture
  runs it is the only quote writer (the ordinary recorder keeps writing
  events), so quotes and books share one cadence instead of two that disagree.
- **Depth**, from the engine's *execution* view - every level the local book
  knows - only while the book is ``SYNCED``, and only when its update id has
  moved. A side truncated to ``depth_levels`` is stored as incomplete, because
  past it the depth is unknown, not absent.
- **Funding**, each poll the tracker received, once.

Every row keeps its own ``local_timestamp`` - the instant *this process*
received it, the clock replay orders by - and a sample is written in **one
transaction**: it lands whole or not at all.

**A failed sample is a recorded gap, never a silent retry.** The rows of a
failed sample are not queued: by the next interval the book has moved, and
writing the old one later would claim a state was captured when it was not.
Instead the recorder remembers the failure, and the next sample that does
land writes a ``DATA_GAP`` system event (component ``replay_capture``) with
the window nothing was captured in, how many samples were lost and why. The
PostgreSQL history source reads those events, and a replay whose range
overlaps one is ``INCOMPLETE``. ``close`` makes a last attempt for a gap still
open at shutdown.

``Settings`` refuses a capture cadence replay would read as stale (see
``Settings._capture_cadence_is_replayable``). ``CaptureHealth`` counts what
this process wrote and lost; ``trading-bot-backtest capture`` reports what the
database holds, from any process.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from sqlalchemy import insert
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from trading_bot.core.config import CaptureConfig
from trading_bot.core.logging import get_logger
from trading_bot.db.models import FundingObservation, MarketData, OrderBookSnapshot, SystemEvent
from trading_bot.db.models.enums import Severity, SystemEventType
from trading_bot.exchange.models import FundingInfo, MarketRef, OrderBook, Quote
from trading_bot.marketdata.models import BookStatus, MarketSnapshot
from trading_bot.marketdata.recorder import quote_row

logger = get_logger(__name__)

SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]
CAPTURE_COMPONENT = "replay_capture"


class ExecutionView(Protocol):
    def execution_snapshot(self, ref: MarketRef) -> MarketSnapshot: ...


class CaptureSnapshotError(RuntimeError):
    """A requested market could not be included in a coherent sample."""

    def __init__(self, ref: MarketRef, cause: Exception) -> None:
        super().__init__(f"{ref}: {type(cause).__name__}: {cause}")
        self.ref = ref


def book_row(market_id: int, book: OrderBook, depth_levels: int) -> dict[str, Any]:
    bids, asks = book.bids[:depth_levels], book.asks[:depth_levels]
    return {
        "market_id": market_id,
        "bids": [[str(level.price), str(level.size)] for level in bids],
        "asks": [[str(level.price), str(level.size)] for level in asks],
        "depth_levels": max(len(bids), len(asks)),
        "bids_complete": book.bids_complete and len(book.bids) <= depth_levels,
        "asks_complete": book.asks_complete and len(book.asks) <= depth_levels,
        "exchange_timestamp": book.exchange_timestamp,
        "local_timestamp": book.local_timestamp,
        "sequence": book.sequence,
    }


def funding_row(market_id: int, funding: FundingInfo) -> dict[str, Any]:
    return {
        "market_id": market_id,
        "mark_price": funding.mark_price,
        "index_price": funding.index_price,
        "funding_rate": funding.last_funding_rate,
        "next_funding_time": funding.next_funding_time,
        "funding_interval_hours": funding.funding_interval_hours,
        "local_timestamp": funding.local_timestamp,
    }


@dataclass(slots=True)
class _Gap:
    started_at: datetime
    samples: int = 0
    markets: set[str] = field(default_factory=set)
    error: str = ""


@dataclass(slots=True)
class CaptureHealth:
    samples_written: int = 0
    quotes_written: int = 0
    books_written: int = 0
    funding_written: int = 0
    failures: int = 0
    samples_lost: int = 0
    gaps_recorded: int = 0
    last_success_at: datetime | None = None
    last_error: str | None = None


@dataclass(slots=True)
class _Sample:
    quotes: list[tuple[MarketRef, Quote, dict[str, Any]]] = field(default_factory=list)
    books: list[tuple[MarketRef, dict[str, Any]]] = field(default_factory=list)
    fundings: list[tuple[MarketRef, dict[str, Any]]] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.quotes or self.books or self.fundings)


def _utcnow() -> datetime:
    return datetime.now(UTC)


class ReplayCaptureRecorder:
    def __init__(
        self,
        market_ids: Mapping[MarketRef, int],
        session_factory: SessionFactory,
        config: CaptureConfig,
        *,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._market_ids = dict(market_ids)
        self._session_factory = session_factory
        self._config = config
        self._clock = clock
        self._last_quote: dict[MarketRef, Quote] = {}
        self._last_sequence: dict[MarketRef, int] = {}
        self._last_funding: dict[MarketRef, datetime] = {}
        self._gap: _Gap | None = None
        self.health = CaptureHealth()

    async def flush(
        self, view: ExecutionView, refs: Sequence[MarketRef], rates: Mapping[MarketRef, FundingInfo]
    ) -> int:
        """Take one sample and write it whole; returns rows written."""
        sampled_at = self._clock()
        try:
            sample = self._sample(view, refs, rates)
        except CaptureSnapshotError as exc:
            self._record_failure(_Sample(), sampled_at, exc, markets=(exc.ref,))
            return 0
        gap = self._gap
        if not sample and gap is None:
            return 0
        try:
            async with self._session_factory() as session:
                await self._write(session, sample, gap, sampled_at)
        except Exception as exc:
            self._record_failure(sample, sampled_at, exc)
            return 0
        self._gap = None
        if gap is not None:
            self.health.gaps_recorded += 1
        for ref, quote, _ in sample.quotes:
            self._last_quote[ref] = quote
        for ref, row in sample.books:
            self._last_sequence[ref] = row["sequence"]
        for ref, row in sample.fundings:
            self._last_funding[ref] = row["local_timestamp"]
        health = self.health
        health.samples_written += 1
        health.quotes_written += len(sample.quotes)
        health.books_written += len(sample.books)
        health.funding_written += len(sample.fundings)
        health.last_success_at = sampled_at
        return len(sample.quotes) + len(sample.books) + len(sample.fundings)

    async def run(
        self,
        view: ExecutionView,
        refs: Sequence[MarketRef],
        rates: Callable[[], Mapping[MarketRef, FundingInfo]],
    ) -> None:
        try:
            while True:
                await asyncio.sleep(self._config.interval_ms / 1000)
                await self.flush(view, refs, rates())
        finally:
            await asyncio.shield(self.close())

    async def close(self) -> None:
        """A last attempt to record a gap still open when capture stops."""
        gap = self._gap
        if gap is None:
            return
        try:
            async with self._session_factory() as session:
                await self._write(session, _Sample(), gap, self._clock())
        except Exception as exc:
            logger.error("capture.gap_unrecorded", started_at=gap.started_at, error=str(exc))
            return
        self._gap = None
        self.health.gaps_recorded += 1

    # --- internals ------------------------------------------------------

    def _sample(
        self, view: ExecutionView, refs: Sequence[MarketRef], rates: Mapping[MarketRef, FundingInfo]
    ) -> _Sample:
        sample = _Sample()
        for ref in refs:
            market_id = self._market_ids.get(ref)
            if market_id is None:
                continue
            try:
                snapshot = view.execution_snapshot(ref)
            except Exception as exc:
                # A partial cross-market sample would look valid but could not
                # reproduce a paired decision. Lose the coherent sample and
                # record the affected market explicitly.
                raise CaptureSnapshotError(ref, exc) from exc
            quote = snapshot.quote
            # Identity, as the quote recorder does: a new object is a new quote.
            if quote is not None and self._last_quote.get(ref) is not quote:
                sample.quotes.append((ref, quote, quote_row(market_id, snapshot, quote)))
            book = snapshot.book
            if (
                snapshot.book_status is BookStatus.SYNCED
                and book is not None
                and book.sequence is not None
                and self._last_sequence.get(ref) != book.sequence
            ):
                sample.books.append((ref, book_row(market_id, book, self._config.depth_levels)))
        sample.fundings = [
            (ref, funding_row(self._market_ids[ref], funding))
            for ref, funding in rates.items()
            if ref in self._market_ids and self._last_funding.get(ref) != funding.local_timestamp
        ]
        return sample

    async def _write(
        self, session: AsyncSession, sample: _Sample, gap: _Gap | None, sampled_at: datetime
    ) -> None:
        if sample.quotes:
            await session.execute(insert(MarketData), [row for _, _, row in sample.quotes])
        if sample.books:
            await session.execute(insert(OrderBookSnapshot), [row for _, row in sample.books])
        if sample.fundings:
            await session.execute(
                pg_insert(FundingObservation)
                .values([row for _, row in sample.fundings])
                .on_conflict_do_nothing(constraint="market_observed_at")
            )
        if gap is not None:
            await session.execute(insert(SystemEvent).values(**_gap_event(gap, sampled_at)))

    def _record_failure(
        self,
        sample: _Sample,
        sampled_at: datetime,
        exc: Exception,
        *,
        markets: Sequence[MarketRef] = (),
    ) -> None:
        health = self.health
        health.failures += 1
        health.samples_lost += 1
        health.last_error = f"{type(exc).__name__}: {exc}"
        gap = self._gap or _Gap(started_at=sampled_at)
        gap.samples += 1
        gap.error = health.last_error
        gap.markets.update(str(ref) for ref, *_ in (*sample.quotes, *sample.books))
        gap.markets.update(str(ref) for ref, _ in sample.fundings)
        gap.markets.update(str(ref) for ref in markets)
        self._gap = gap
        logger.warning(
            "capture.sample_lost",
            error=health.last_error,
            gap_started_at=gap.started_at.isoformat(),
            samples_lost=gap.samples,
        )


def _gap_event(gap: _Gap, ended_at: datetime) -> dict[str, Any]:
    return {
        "occurred_at": gap.started_at,
        "event_type": SystemEventType.DATA_GAP,
        "severity": Severity.WARNING,
        "component": CAPTURE_COMPONENT,
        "message": (
            f"replay capture wrote nothing from {gap.started_at.isoformat()} to "
            f"{ended_at.isoformat()}: {gap.samples} sample(s) lost ({gap.error})"
        ),
        "context": {
            "gap_start": gap.started_at.isoformat(),
            "gap_end": ended_at.isoformat(),
            "samples_lost": gap.samples,
            "markets": sorted(gap.markets),
            "error": gap.error,
        },
    }
