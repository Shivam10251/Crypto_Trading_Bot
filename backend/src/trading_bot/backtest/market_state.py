"""Recorded market data, as the pipeline would have seen it at virtual time T.

``ReplayMarketData`` answers the same read API as ``MarketDataEngine`` -
``snapshot``, ``execution_snapshot``, ``snapshots`` - so the monitor, the
strategy runner, the paper simulator and the portfolio's mark reader run
against it unchanged.

**Nothing from after T is visible, by construction.** Events wait in a
lookahead buffer and are applied only when a read happens at a virtual time at
or after their ``available_at``. A read is refused outright - not answered
from a partial view - unless the buffer provably holds every event up to that
instant: either the source is exhausted, or an event strictly later than now
is already buffered. That is what lets an order's simulated latency move the
clock mid-execution and still read exactly the book the recorded feed had
delivered by its arrival, and never one delivered after.

**Nothing is carried forward without bound.** A sampled book is published
until the next sample arrives, and ages meanwhile - which the strategy, the
simulator and valuation all already refuse past their limits. Beyond
``max_book_carry_ms`` it is withdrawn entirely (``SYNCING``, no book), and a
funding observation beyond ``max_funding_carry_ms`` is forgotten, each counted
as a dataset issue. A market whose last event is older than
``market_silence_ms`` reads ``STALE``, exactly as the live engine's per-market
rule would.

**State in force at the start is initialization, not history.** A replay
starting between two samples must not open blind: ``initialize`` applies the
newest valid quote, book and funding observation received *before* the start
(within the carry limits), as the live engine would already hold them. They
are applied at the start instant - never before their own receipt - and are
counted apart from in-window events.

**A violated invariant cannot be swallowed.** Several readers - the paper
simulator, the exit mark reader - turn a failing feed into "no book". A read
refused for lack of lookahead is not that: it is recorded in ``violations``
before it is raised, and the engine fails the run on it.

**What a recording cannot supply is absent, not invented.** No 24h ticker was
recorded, so ``last_price`` and quote volume are ``None``; there are no
connections, so ``DISCONNECTED`` never occurs and gaps show up as ageing data.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from trading_bot.backtest.clock import ReplayInvariantError
from trading_bot.backtest.events import EventKind, ReplayEvent
from trading_bot.backtest.source import DatasetIssues
from trading_bot.exchange.errors import UnknownMarketError
from trading_bot.exchange.models import FundingInfo, MarketRef, OrderBook, Quote
from trading_bot.marketdata.models import BookLiquidity, BookStatus, FeedStatus, MarketSnapshot
from trading_bot.marketdata.order_book import BookSyncError, LocalOrderBook


@dataclass(frozen=True, slots=True)
class CarryLimits:
    depth_levels: int
    market_silence_ms: int
    max_book_carry_ms: int
    max_funding_carry_ms: int
    liquidity_band_bps: Decimal
    reference_notional: Decimal


@dataclass(frozen=True, slots=True)
class SettlementObservation:
    """The last funding observation received before one settlement."""

    settles_at: datetime
    observed_at: datetime
    rate: Decimal
    mark_price: Decimal
    interval_hours: int | None


class _Market:
    __slots__ = (
        "book",
        "book_views",
        "funding",
        "gaps",
        "last_update_at",
        "quote",
        "ref",
        "settlements",
        "updates",
        "volume_24h",
    )

    def __init__(self, ref: MarketRef) -> None:
        self.ref = ref
        self.quote: Quote | None = None
        self.volume_24h: Decimal | None = None
        self.book: OrderBook | None = None
        self.book_views: tuple[OrderBook, BookLiquidity | None] | None = None
        self.funding: FundingInfo | None = None
        self.settlements: dict[datetime, SettlementObservation] = {}
        self.last_update_at: datetime | None = None
        self.updates = 0
        self.gaps = 0


def _ms(later: datetime, earlier: datetime) -> int:
    return int((later - earlier).total_seconds() * 1000)


class ReplayMarketData:
    def __init__(
        self,
        refs: Sequence[MarketRef],
        clock: Callable[[], datetime],
        limits: CarryLimits,
        issues: DatasetIssues,
    ) -> None:
        self._refs = tuple(dict.fromkeys(refs))
        self._clock = clock
        self._limits = limits
        self._issues = issues
        self._markets = {ref: _Market(ref) for ref in self._refs}
        self._buffer: deque[ReplayEvent] = deque()
        self._exhausted = False
        self.violations: list[str] = []
        self.initialization_applied = 0
        self.events_applied = 0
        self.first_applied_at: datetime | None = None
        self.last_applied_at: datetime | None = None

    # --- feeding --------------------------------------------------------

    def extend(self, events: Iterable[ReplayEvent]) -> None:
        """Buffer validated events, in order. They stay invisible until due."""
        for event in events:
            if self._buffer and event.order_key < self._buffer[-1].order_key:
                raise ReplayInvariantError("events must be buffered in replay order")
            self._buffer.append(event)

    def initialize(self, events: Iterable[ReplayEvent]) -> None:
        """Apply the state in force at the start. Only before anything is buffered."""
        if self._buffer or self.events_applied:
            raise ReplayInvariantError("initialization must precede every in-window event")
        now = self._clock()
        for event in events:
            if event.available_at >= now:
                raise ReplayInvariantError(
                    f"initialization event at {event.available_at.isoformat()} is not before "
                    f"the start {now.isoformat()}"
                )
            self._apply(event, initialization=True)

    def mark_exhausted(self) -> None:
        self._exhausted = True

    @property
    def is_exhausted(self) -> bool:
        return self._exhausted

    @property
    def buffered(self) -> int:
        return len(self._buffer)

    @property
    def buffered_until(self) -> datetime | None:
        return self._buffer[-1].available_at if self._buffer else None

    def covers(self, moment: datetime) -> bool:
        """Whether every event up to and including ``moment`` is buffered or applied."""
        last = self.buffered_until
        return self._exhausted or (last is not None and last > moment)

    # --- reading (the MarketDataEngine API) -----------------------------

    @property
    def refs(self) -> tuple[MarketRef, ...]:
        return self._refs

    def snapshot(self, ref: MarketRef) -> MarketSnapshot:
        return self._snapshot(ref, execution=False)

    def execution_snapshot(self, ref: MarketRef) -> MarketSnapshot:
        """Every recorded depth level, for the simulator to walk."""
        return self._snapshot(ref, execution=True)

    def snapshots(self) -> list[MarketSnapshot]:
        return [self.snapshot(ref) for ref in self._refs]

    @property
    def rates(self) -> dict[MarketRef, FundingInfo]:
        """Current funding per perpetual, for ``StrategyRunner.set_funding``."""
        self.catch_up()
        return {ref: market.funding for ref, market in self._markets.items() if market.funding}

    def mark_price(self, ref: MarketRef) -> Decimal | None:
        """The recorded mark, for the venue's MARKET notional check."""
        self.catch_up()
        market = self._markets.get(ref)
        return market.funding.mark_price if market and market.funding else None

    def settlement_observations(self, ref: MarketRef) -> dict[datetime, SettlementObservation]:
        """Per settlement instant, the last observation received before it."""
        self.catch_up()
        market = self._markets.get(ref)
        return dict(market.settlements) if market else {}

    # --- applying -------------------------------------------------------

    def catch_up(self) -> None:
        now = self._clock()
        if not self.covers(now):
            message = (
                f"replay read at {now.isoformat()} but events are only buffered until "
                f"{self.buffered_until}; the lookahead horizon is too short"
            )
            self.violations.append(message)
            raise ReplayInvariantError(message)
        while self._buffer and self._buffer[0].available_at <= now:
            self._apply(self._buffer.popleft())
        self._expire(now)

    def _apply(self, event: ReplayEvent, *, initialization: bool = False) -> None:
        market = self._markets.get(event.ref)
        if market is None:  # the source filters by market; a guard, not a path
            return
        payload = event.payload
        if event.kind is EventKind.QUOTE and isinstance(payload, Quote):
            market.quote = payload
            if event.volume_24h is not None:
                market.volume_24h = event.volume_24h
        elif event.kind is EventKind.BOOK and isinstance(payload, OrderBook):
            market.book = payload
            market.book_views = None
        elif isinstance(payload, FundingInfo):
            market.funding = payload
            settles_at = payload.next_funding_time
            if event.available_at <= settles_at:
                # Later observations of the same settlement overwrite earlier
                # ones, so this keeps the last word before it settled.
                market.settlements[settles_at] = SettlementObservation(
                    settles_at=settles_at,
                    observed_at=event.available_at,
                    rate=payload.last_funding_rate,
                    mark_price=payload.mark_price,
                    interval_hours=payload.funding_interval_hours,
                )
        market.last_update_at = event.available_at
        market.updates += 1
        if initialization:
            self.initialization_applied += 1
            return
        self.events_applied += 1
        self.first_applied_at = self.first_applied_at or event.available_at
        self.last_applied_at = event.available_at

    def _expire(self, now: datetime) -> None:
        limits = self._limits
        for market in self._markets.values():
            book = market.book
            if book is not None and _ms(now, book.local_timestamp) > limits.max_book_carry_ms:
                self._issues.add(
                    "book_carry_expired",
                    f"{market.ref} book from {book.local_timestamp.isoformat()} withdrawn at "
                    f"{now.isoformat()}",
                )
                market.gaps += 1
                market.book = None
                market.book_views = None
            funding = market.funding
            if funding is not None and (
                _ms(now, funding.local_timestamp) > limits.max_funding_carry_ms
            ):
                self._issues.add(
                    "funding_carry_expired",
                    f"{market.ref} funding from {funding.local_timestamp.isoformat()} forgotten "
                    f"at {now.isoformat()}",
                )
                market.funding = None

    def _snapshot(self, ref: MarketRef, *, execution: bool) -> MarketSnapshot:
        market = self._markets.get(ref)
        if market is None:
            raise UnknownMarketError(f"{ref} is not part of this replay")
        self.catch_up()
        now = self._clock()
        quote = market.quote
        book = market.book
        published: OrderBook | None = None
        liquidity: BookLiquidity | None = None
        if book is not None:
            published, liquidity = self._views(market, book)
            if execution:
                published = book
        status = FeedStatus.CONNECTING
        if market.last_update_at is not None:
            silent = _ms(now, market.last_update_at) > self._limits.market_silence_ms
            status = FeedStatus.STALE if silent or quote is None else FeedStatus.LIVE
        # Exchange-to-local delay of the newest input that carried a venue
        # clock, as the engine reports it; ``None`` when none did.
        latency = next(
            (
                _ms(item.local_timestamp, item.exchange_timestamp)
                for item in (book, quote)
                if item is not None and item.exchange_timestamp is not None
            ),
            None,
        )
        return MarketSnapshot(
            ref=ref,
            status=status,
            quote=quote,
            book=published,
            book_status=BookStatus.SYNCED if published is not None else BookStatus.SYNCING,
            last_price=None,
            volume_24h=market.volume_24h,
            quote_volume_24h=None,
            latency_ms=latency,
            last_update_at=market.last_update_at,
            age_ms=_ms(now, market.last_update_at) if market.last_update_at else None,
            quote_age_ms=_ms(now, quote.local_timestamp) if quote is not None else None,
            book_age_ms=_ms(now, book.local_timestamp) if published is not None and book else None,
            updates=market.updates,
            gaps=market.gaps,
            resyncs=0,
            liquidity=liquidity,
        )

    def _views(self, market: _Market, book: OrderBook) -> tuple[OrderBook, BookLiquidity | None]:
        """The top ``depth_levels`` the engine would publish, and its liquidity."""
        if market.book_views is not None:
            return market.book_views
        levels = self._limits.depth_levels
        top = OrderBook(
            ref=book.ref,
            bids=book.bids[:levels],
            asks=book.asks[:levels],
            local_timestamp=book.local_timestamp,
            exchange_timestamp=book.exchange_timestamp,
            sequence=book.sequence,
            bids_complete=book.bids_complete and len(book.bids) <= levels,
            asks_complete=book.asks_complete and len(book.asks) <= levels,
        )
        liquidity: BookLiquidity | None
        try:
            local = LocalOrderBook(book.ref, min_levels=1)
            local.load_snapshot(book)
            liquidity = local.liquidity(
                self._limits.liquidity_band_bps, self._limits.reference_notional
            )
        except BookSyncError:
            liquidity = None
        market.book_views = (top, liquidity)
        return market.book_views


def horizon_for(latencies_ms: Iterable[int], jitter_ms: int) -> timedelta:
    """How far past a decision the buffer must reach for an order to arrive."""
    return timedelta(milliseconds=max(latencies_ms, default=0) + jitter_ms + 1)
