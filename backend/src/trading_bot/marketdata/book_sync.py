"""Order-book synchronisation: one rebuildable local book per market.

The engine hands every depth update here and asks for a book to publish. This
module owns everything in between:

- buffering updates while a REST snapshot is in flight
- bridging the snapshot with the buffer (``LocalOrderBook`` enforces the
  venue's sequencing rules)
- invalidating a book on a sequence gap, a crossed book or a disconnect, so
  consumers see ``SYNCING`` rather than a wrong book
- rate-limiting rebuilds: each snapshot costs REST weight, so rebuilds are
  spaced per market and capped in flight across all of them - a reconnect that
  invalidates fifty books must not fire fifty snapshot requests at once
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from decimal import Decimal

from trading_bot.core.config import MarketDataConfig
from trading_bot.core.logging import get_logger
from trading_bot.exchange.errors import ExchangeError
from trading_bot.exchange.models import DepthDiff, MarketRef, OrderBook
from trading_bot.marketdata.models import BookLiquidity, BookStatus
from trading_bot.marketdata.order_book import BookSyncError, DepthExhaustedError, LocalOrderBook

logger = get_logger(__name__)

SnapshotFetcher = Callable[[MarketRef, int], Awaitable[OrderBook]]
# (market, reason, integrity). Integrity is False for routine rebuilds - a
# disconnect, or the market moving past the snapshot's price range.
InvalidationHandler = Callable[[MarketRef, str, bool], None]

# A rebuild waits this long for the stream to buffer an update before looking
# again: a snapshot is useless until an update can bridge it.
_BUFFER_WAIT_SECONDS = 1.0
_MAX_REBUILD_BACKOFF_SECONDS = 30.0


@dataclass(frozen=True, slots=True)
class BookView:
    """What the engine may publish for one market's book."""

    book: OrderBook | None
    status: BookStatus
    liquidity: BookLiquidity | None = None


DISABLED_VIEW = BookView(book=None, status=BookStatus.DISABLED)


class _Book:
    """One market's local book plus what it takes to rebuild it."""

    __slots__ = (
        "buffer",
        "buffered",
        "cached_execution",
        "cached_liquidity",
        "cached_top",
        "failures",
        "last_attempt",
        "local",
        "ref",
        "status",
        "task",
    )

    def __init__(self, ref: MarketRef, *, min_levels: int, max_buffered: int) -> None:
        self.ref = ref
        self.local = LocalOrderBook(ref, min_levels=min_levels)
        self.status = BookStatus.SYNCING
        self.buffer: deque[DepthDiff] = deque(maxlen=max_buffered)
        self.buffered = asyncio.Event()
        # Derived views, computed when first read after an update.
        self.cached_top: OrderBook | None = None
        self.cached_execution: OrderBook | None = None
        self.cached_liquidity: BookLiquidity | None = None
        self.task: asyncio.Task[None] | None = None
        # Event-loop time of the last snapshot request, for the rate floor.
        self.last_attempt: float | None = None
        self.failures = 0

    def push(self, diff: DepthDiff) -> None:
        self.buffer.append(diff)
        self.buffered.set()

    def forget_views(self) -> None:
        self.cached_top = None
        self.cached_execution = None
        self.cached_liquidity = None

    def discard(self) -> None:
        self.status = BookStatus.SYNCING
        self.local.reset()
        self.buffer.clear()
        self.buffered.clear()
        self.forget_views()


class BookSynchronizer:
    """Local books for a set of markets, each rebuilt independently."""

    def __init__(
        self,
        refs: Iterable[MarketRef],
        *,
        depth_levels: int,
        config: MarketDataConfig,
        fetch_snapshot: SnapshotFetcher,
        on_invalidated: InvalidationHandler,
        on_synced: Callable[[MarketRef], None],
        on_snapshot_failed: Callable[[MarketRef, ExchangeError], None],
    ) -> None:
        self._depth_levels = depth_levels
        self._config = config
        self._band_bps = Decimal(str(config.liquidity_band_bps))
        self._reference_notional = Decimal(str(config.reference_order_notional))
        self._fetch = fetch_snapshot
        self._on_invalidated = on_invalidated
        self._on_synced = on_synced
        self._on_snapshot_failed = on_snapshot_failed
        self._snapshot_slots = asyncio.Semaphore(config.max_concurrent_snapshots)
        self._books = {
            ref: _Book(ref, min_levels=depth_levels, max_buffered=config.max_buffered_updates)
            for ref in refs
        }
        self._running = False

    def __contains__(self, ref: object) -> bool:
        return ref in self._books

    def __len__(self) -> int:
        return len(self._books)

    @property
    def synced(self) -> int:
        return sum(book.status is BookStatus.SYNCED for book in self._books.values())

    def start(self) -> None:
        self._running = True

    async def stop(self) -> None:
        self._running = False
        tasks = [book.task for book in self._books.values() if book.task is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for book in self._books.values():
            book.task = None

    # --- engine-facing ----------------------------------------------------

    def apply(self, diff: DepthDiff) -> None:
        book = self._books.get(diff.ref)
        if book is None:
            return
        if book.status is not BookStatus.SYNCED:
            book.push(diff)
            self._ensure_rebuild(book)
            return
        try:
            book.local.apply(diff)
        except BookSyncError as exc:
            self._invalidate(book, exc)
            # The update that exposed the problem is still valid for the rebuild.
            book.push(diff)
            self._ensure_rebuild(book)
        else:
            book.forget_views()

    def view(self, ref: MarketRef) -> BookView:
        """The book to publish, or the reason there is none."""
        book = self._books.get(ref)
        if book is None:
            return DISABLED_VIEW
        if book.status is not BookStatus.SYNCED:
            return BookView(book=None, status=book.status)
        if book.cached_top is None:
            try:
                book.cached_top = book.local.top(self._depth_levels)
                book.cached_liquidity = book.local.liquidity(
                    self._band_bps, self._reference_notional
                )
            except BookSyncError as exc:
                # Validated lazily, when someone reads, so the hot path does not
                # pay for a sort on every update.
                self._invalidate(book, exc)
                self._ensure_rebuild(book)
                return BookView(book=None, status=BookStatus.SYNCING)
        return BookView(
            book=book.cached_top, status=BookStatus.SYNCED, liquidity=book.cached_liquidity
        )

    def execution_view(self, ref: MarketRef) -> BookView:
        """Every known level for conservative execution simulation."""
        book = self._books.get(ref)
        if book is None:
            return DISABLED_VIEW
        if book.status is not BookStatus.SYNCED:
            return BookView(book=None, status=book.status)
        try:
            if book.cached_execution is None:
                book.cached_execution = book.local.all_known()
            return BookView(book=book.cached_execution, status=BookStatus.SYNCED)
        except BookSyncError as exc:
            self._invalidate(book, exc)
            self._ensure_rebuild(book)
            return BookView(book=None, status=BookStatus.SYNCING)

    def invalidate(self, ref: MarketRef, reason: str) -> None:
        """Routine invalidation, such as the depth stream disconnecting."""
        book = self._books.get(ref)
        if book is not None:
            book.discard()
            self._on_invalidated(ref, reason, False)

    # --- rebuilding -------------------------------------------------------

    def _invalidate(self, book: _Book, exc: BookSyncError) -> None:
        book.discard()
        self._on_invalidated(book.ref, str(exc), not isinstance(exc, DepthExhaustedError))

    def _ensure_rebuild(self, book: _Book) -> None:
        if not self._running:
            return
        if book.task is None or book.task.done():
            book.task = asyncio.create_task(
                self._rebuild(book), name=f"market-data:rebuild:{book.ref}"
            )

    async def _rebuild(self, book: _Book) -> None:
        """Retry snapshot + bridge until the book holds."""
        loop = asyncio.get_running_loop()
        while True:
            delay = 0.0
            if book.last_attempt is not None:
                elapsed = loop.time() - book.last_attempt
                delay = self._config.resync_min_interval_seconds - elapsed
            if book.failures:
                backoff = 0.5 * 2.0 ** min(book.failures, 10)
                delay = max(delay, min(_MAX_REBUILD_BACKOFF_SECONDS, backoff))
            if delay > 0:
                await asyncio.sleep(delay)
            if not book.buffer:
                book.buffered.clear()
                try:
                    await asyncio.wait_for(book.buffered.wait(), timeout=_BUFFER_WAIT_SECONDS)
                except TimeoutError:
                    continue
            async with self._snapshot_slots:
                book.last_attempt = loop.time()
                try:
                    snapshot = await self._fetch(book.ref, self._config.snapshot_depth)
                except ExchangeError as exc:
                    book.failures += 1
                    self._on_snapshot_failed(book.ref, exc)
                    continue
            if self._bridge(book, snapshot):
                return

    def _bridge(self, book: _Book, snapshot: OrderBook) -> bool:
        """Load ``snapshot`` and replay the buffer onto it. No awaits: atomic."""
        if not book.buffer or snapshot.sequence is None:
            return False
        first = book.buffer[0].first_update_id
        if snapshot.sequence < first - 1:
            # The snapshot predates every buffered update, so whatever happened
            # in between is lost. Binance's procedure: take another snapshot.
            logger.debug(
                "market_data.snapshot_too_old",
                market=str(book.ref),
                snapshot=snapshot.sequence,
                first_buffered=first,
            )
            return False
        try:
            book.local.load_snapshot(snapshot)
            for diff in book.buffer:
                book.local.apply(diff)
        except BookSyncError as exc:
            book.failures += 1
            book.discard()
            logger.warning(
                "market_data.bridge_failed",
                market=str(book.ref),
                error=str(exc),
                failures=book.failures,
            )
            return False
        book.buffer.clear()
        book.status = BookStatus.SYNCED
        book.forget_views()
        book.failures = 0
        logger.info(
            "market_data.book_synced", market=str(book.ref), update_id=book.local.last_update_id
        )
        self._on_synced(book.ref)
        return True
