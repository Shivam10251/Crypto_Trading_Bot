"""A local order book kept in sync from a snapshot plus incremental updates.

Binance's documented procedure, implemented for spot and USD-M futures alike
(recorded live sequences in tests/fixtures/binance prove both variants):

1. Buffer depth updates from the stream.
2. Fetch a REST snapshot; call its ``lastUpdateId`` ``L``.
3. Ignore updates with ``u < L`` - the snapshot already contains them.
4. The first update applied must straddle the snapshot: ``U <= L + 1``.
5. Every later update must follow without a gap: spot ``U == previous u + 1``;
   futures ``pu == previous u`` (futures ids are not contiguous).
6. Any violation means an update was lost: discard the book and rebuild.

A snapshot only covers N levels, so the book is only *known* inside the price
range that snapshot spanned. Levels beyond it are unknown, not absent. Updates
outside the range are therefore ignored, and when trading carries the market
past the range the book asks to be rebuilt rather than publishing holes.
"""

from __future__ import annotations

import heapq
from collections.abc import Iterator
from datetime import datetime
from decimal import Decimal

from trading_bot.exchange.models import BPS_SCALE, BookLevel, DepthDiff, MarketRef, OrderBook
from trading_bot.marketdata.models import BookLiquidity


class BookSyncError(Exception):
    """The local book can no longer be trusted and must be rebuilt."""


class SequenceGapError(BookSyncError):
    """An update is missing between the last one applied and this one."""


class DepthExhaustedError(BookSyncError):
    """The market moved beyond the price range the snapshot covered."""


def _set_level(side: dict[Decimal, Decimal], level: BookLevel) -> None:
    if level.size == 0:
        side.pop(level.price, None)
    else:
        side[level.price] = level.size


class LocalOrderBook:
    def __init__(self, ref: MarketRef, *, min_levels: int) -> None:
        if min_levels < 1:
            raise ValueError("min_levels must be positive")
        self.ref = ref
        self._min_levels = min_levels
        self._bids: dict[Decimal, Decimal] = {}
        self._asks: dict[Decimal, Decimal] = {}
        # Every price between the best level and these bounds is known.
        self._bid_floor = Decimal(0)
        self._ask_ceiling = Decimal(0)
        self._last_update_id: int | None = None
        self._bridged = False
        self.exchange_timestamp: datetime | None = None
        self.local_timestamp: datetime | None = None

    @property
    def is_loaded(self) -> bool:
        return self._last_update_id is not None

    @property
    def last_update_id(self) -> int | None:
        return self._last_update_id

    def reset(self) -> None:
        self._bids.clear()
        self._asks.clear()
        self._last_update_id = None
        self._bridged = False
        self.exchange_timestamp = None
        self.local_timestamp = None

    def load_snapshot(self, snapshot: OrderBook) -> None:
        if snapshot.ref != self.ref:
            raise ValueError(f"snapshot for {snapshot.ref} loaded into book for {self.ref}")
        if snapshot.sequence is None:
            raise BookSyncError(f"{self.ref}: snapshot has no update id to synchronise from")
        self._bids = {level.price: level.size for level in snapshot.bids}
        self._asks = {level.price: level.size for level in snapshot.asks}
        self._bid_floor = snapshot.bids[-1].price
        self._ask_ceiling = snapshot.asks[-1].price
        self._last_update_id = snapshot.sequence
        self._bridged = False
        self.exchange_timestamp = snapshot.exchange_timestamp
        self.local_timestamp = snapshot.local_timestamp

    def apply(self, diff: DepthDiff) -> bool:
        """Apply one update; ``False`` when the book already reflected it.

        Raises ``SequenceGapError`` when an update is missing and
        ``DepthExhaustedError`` when too few known levels remain - either way
        the caller must rebuild from a fresh snapshot.
        """
        if self._last_update_id is None:
            raise BookSyncError(f"{self.ref}: no snapshot loaded")
        if diff.ref != self.ref:
            raise ValueError(f"update for {diff.ref} applied to book for {self.ref}")

        last = self._last_update_id
        if not self._bridged:
            if diff.final_update_id < last:
                return False
            if diff.first_update_id > last + 1:
                raise SequenceGapError(
                    f"{self.ref}: first update starts at {diff.first_update_id} "
                    f"but the snapshot ends at {last}"
                )
        else:
            if diff.final_update_id <= last:
                return False  # duplicate or replay
            if diff.previous_final_update_id is not None:
                continuous = diff.previous_final_update_id == last
            else:
                continuous = diff.first_update_id == last + 1
            if not continuous:
                raise SequenceGapError(
                    f"{self.ref}: expected the update after {last}, got "
                    f"U={diff.first_update_id} pu={diff.previous_final_update_id}"
                )

        for level in diff.bids:
            if level.price >= self._bid_floor:
                _set_level(self._bids, level)
        for level in diff.asks:
            if level.price <= self._ask_ceiling:
                _set_level(self._asks, level)
        self._last_update_id = diff.final_update_id
        self._bridged = True
        if diff.exchange_timestamp is not None:
            self.exchange_timestamp = diff.exchange_timestamp
        self.local_timestamp = diff.local_timestamp

        if len(self._bids) < self._min_levels or len(self._asks) < self._min_levels:
            raise DepthExhaustedError(
                f"{self.ref}: fewer than {self._min_levels} known levels remain "
                f"({len(self._bids)} bids, {len(self._asks)} asks)"
            )
        return True

    def top(self, levels: int) -> OrderBook:
        """The best ``levels`` on each side as an immutable ``OrderBook``.

        The crossed-book check lives here rather than in ``apply`` so the hot
        path avoids a scan per update; a crossed local book is corruption, not
        a market state, and raises.
        """
        if self._last_update_id is None or self.local_timestamp is None:
            raise BookSyncError(f"{self.ref}: no snapshot loaded")
        bids = heapq.nlargest(levels, self._bids.items())
        asks = heapq.nsmallest(levels, self._asks.items())
        if len(bids) < levels or len(asks) < levels:
            raise DepthExhaustedError(f"{self.ref}: fewer than {levels} known levels")
        if bids[0][0] >= asks[0][0]:
            raise BookSyncError(
                f"{self.ref}: local book crossed (bid {bids[0][0]} >= ask {asks[0][0]})"
            )
        return OrderBook(
            ref=self.ref,
            bids=tuple(BookLevel(price, size) for price, size in bids),
            asks=tuple(BookLevel(price, size) for price, size in asks),
            local_timestamp=self.local_timestamp,
            exchange_timestamp=self.exchange_timestamp,
            sequence=self._last_update_id,
        )

    def liquidity(self, band_bps: Decimal, reference_notional: Decimal) -> BookLiquidity:
        """Resting value near the mid, and what a reference-size order would pay.

        Measured over every known level, not only the published top, so a
        deep band on a liquid market is measured rather than truncated.
        ``*_complete`` says whether the band stayed inside the price range the
        snapshot covered; beyond it the book is unknown, so the figure is a
        lower bound.
        """
        if self._last_update_id is None or not self._bids or not self._asks:
            raise BookSyncError(f"{self.ref}: no snapshot loaded")
        best_bid, best_ask = max(self._bids), min(self._asks)
        if best_bid >= best_ask:
            raise BookSyncError(
                f"{self.ref}: local book crossed (bid {best_bid} >= ask {best_ask})"
            )
        mid = (best_bid + best_ask) / 2
        reach = mid * band_bps / BPS_SCALE
        low, high = mid - reach, mid + reach
        return BookLiquidity(
            band_bps=band_bps,
            bid_notional=sum((p * s for p, s in self._bids.items() if p >= low), Decimal(0)),
            ask_notional=sum((p * s for p, s in self._asks.items() if p <= high), Decimal(0)),
            bid_complete=self._bid_floor <= low,
            ask_complete=self._ask_ceiling >= high,
            reference_notional=reference_notional,
            buy_slippage_bps=_slippage_bps(
                _best_first(self._asks, ascending=True), mid, reference_notional, buy=True
            ),
            sell_slippage_bps=_slippage_bps(
                _best_first(self._bids, ascending=False), mid, reference_notional, buy=False
            ),
        )


def _best_first(
    side: dict[Decimal, Decimal], *, ascending: bool
) -> Iterator[tuple[Decimal, Decimal]]:
    """Levels best price first, without sorting the side: most walks stop early."""
    heap = [(price if ascending else -price, price, size) for price, size in side.items()]
    heapq.heapify(heap)
    while heap:
        _, price, size = heapq.heappop(heap)
        yield price, size


def _slippage_bps(
    levels: Iterator[tuple[Decimal, Decimal]], mid: Decimal, notional: Decimal, *, buy: bool
) -> Decimal | None:
    """Average fill distance from ``mid``, in bps, of spending ``notional``."""
    remaining = notional
    quantity = Decimal(0)
    for price, size in levels:
        value = price * size
        if value >= remaining:
            quantity += remaining / price
            remaining = Decimal(0)
            break
        quantity += size
        remaining -= value
    if remaining > 0:
        return None
    average = notional / quantity
    distance = average - mid if buy else mid - average
    return distance / mid * BPS_SCALE
