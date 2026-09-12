"""Pricing an exit against the book that would actually have to absorb it.

A position is worth what flattening it would fetch, not what the mid says.
The mid is the average of two prices nobody is obliged to trade at; walking
the book for the exact residual quantity is the only figure that survives
contact with a venue, and it is what both the exit policy and the portfolio
valuation use.

Three refusals, and none of them falls back to a number:

- **no feed, not live, or the book is not SYNCED** - there is no price
- **a book older than ``max_book_age_ms``** - there was a price, and it may
  not be there now
- **not enough depth for the whole quantity** - there is a price for part of
  it, reported as incomplete, and a caller decides whether a partial exit is
  what it wanted

``ExecutableExit.price`` is ``None`` in the first two cases and in the third
only when nothing at all could fill. A valuation that cannot price a position
says so; it never substitutes the last price it saw.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from trading_bot.db.models.enums import MarketType, Side
from trading_bot.exchange.models import MarketRef
from trading_bot.execution.base import MarketFeed
from trading_bot.marketdata.models import BookStatus, MarketSnapshot


class ExitPricingProblem:
    """Why a book could not price an exit. Values are stored in audit rows."""

    NO_FEED = "NO_FEED"
    NOT_LIVE = "NOT_LIVE"
    BOOK_NOT_SYNCED = "BOOK_NOT_SYNCED"
    STALE_BOOK = "STALE_BOOK"
    NO_LIQUIDITY = "NO_LIQUIDITY"
    INSUFFICIENT_DEPTH = "INSUFFICIENT_DEPTH"
    DEPTH_TRUNCATED = "DEPTH_TRUNCATED"


@dataclass(frozen=True, slots=True)
class ExecutableExit:
    """What the current book would pay (or charge) to flatten ``quantity``."""

    ref: MarketRef
    #: The side we would have to trade to reduce the position.
    side: Side
    quantity: Decimal
    #: VWAP of the walk. ``None`` when nothing could be priced at all.
    price: Decimal | None
    #: How much of ``quantity`` the visible book could absorb.
    fillable: Decimal
    #: True only when the walk covered the whole quantity from known depth.
    complete: bool
    problem: str | None = None
    book_sequence: int | None = None
    book_local_timestamp: datetime | None = None
    book_age_ms: int | None = None

    @property
    def is_priced(self) -> bool:
        """A usable price for the whole quantity - the only case that values."""
        return self.price is not None and self.complete


def exit_side(entry_side: Side) -> Side:
    """The side that reduces a position entered on ``entry_side``."""
    return Side.SELL if entry_side is Side.BUY else Side.BUY


def price_exit(
    ref: MarketRef,
    snapshot: MarketSnapshot | None,
    *,
    entry_side: Side,
    quantity: Decimal,
    max_book_age_ms: int,
) -> ExecutableExit:
    """Walk the current book for the quantity that would flatten a position."""
    side = exit_side(entry_side)

    def refuse(problem: str) -> ExecutableExit:
        return ExecutableExit(
            ref=ref,
            side=side,
            quantity=quantity,
            price=None,
            fillable=Decimal(0),
            complete=False,
            problem=problem,
        )

    if snapshot is None:
        return refuse(ExitPricingProblem.NO_FEED)
    if not snapshot.is_live:
        return refuse(ExitPricingProblem.NOT_LIVE)
    if snapshot.book_status is not BookStatus.SYNCED or snapshot.book is None:
        return refuse(ExitPricingProblem.BOOK_NOT_SYNCED)
    age = snapshot.book_age_ms
    if age is not None and age > max_book_age_ms:
        return refuse(ExitPricingProblem.STALE_BOOK)
    if quantity <= 0:
        return refuse(ExitPricingProblem.NO_LIQUIDITY)

    book = snapshot.book
    fill = book.walk(side, quantity)
    complete = book.asks_complete if side is Side.BUY else book.bids_complete
    if fill.filled <= 0:
        return ExecutableExit(
            ref=ref,
            side=side,
            quantity=quantity,
            price=None,
            fillable=Decimal(0),
            complete=False,
            problem=ExitPricingProblem.NO_LIQUIDITY,
            book_sequence=book.sequence,
            book_local_timestamp=book.local_timestamp,
            book_age_ms=age,
        )
    filled_all = fill.is_complete
    return ExecutableExit(
        ref=ref,
        side=side,
        quantity=quantity,
        price=fill.average_price,
        fillable=fill.filled,
        complete=filled_all,
        problem=(
            None
            if filled_all
            else (
                ExitPricingProblem.INSUFFICIENT_DEPTH
                if complete
                else ExitPricingProblem.DEPTH_TRUNCATED
            )
        ),
        book_sequence=book.sequence,
        book_local_timestamp=book.local_timestamp,
        book_age_ms=age,
    )


class MarkReader:
    """Executable exit prices for open legs, read from the live feed.

    Wraps ``MarketFeed`` so nothing downstream has to defend against a feed
    that raises: an exception from the engine becomes an unpriceable exit,
    which is the same outcome as a book that is not there.
    """

    def __init__(self, feed: MarketFeed, *, max_book_age_ms: int) -> None:
        self._feed = feed
        self._max_book_age_ms = max_book_age_ms

    def snapshot(self, ref: MarketRef) -> MarketSnapshot | None:
        try:
            return self._feed.snapshot(ref)
        except Exception:  # a dead feed is an unpriceable exit, not a crash
            return None

    def executable_exit(
        self, ref: MarketRef, *, entry_side: Side, quantity: Decimal
    ) -> ExecutableExit:
        return price_exit(
            ref,
            self.snapshot(ref),
            entry_side=entry_side,
            quantity=quantity,
            max_book_age_ms=self._max_book_age_ms,
        )


def position_value_usd(
    market_type: MarketType,
    *,
    entry_side: Side,
    entry_price: Decimal,
    quantity: Decimal,
    executable_price: Decimal,
) -> Decimal:
    """What one open leg contributes to ``equity = cash + position value``.

    The two instrument classes contribute differently because the paper
    account holds them differently, and getting this wrong double-counts a
    whole leg:

    - **Spot** moved cash when it was bought or sold, so the holding itself is
      the asset: a long leg is worth what selling it would fetch, and a
      borrowed short leg is a liability of what buying it back would cost.
    - **Perpetual** moved no cash - its margin is reserved against cash rather
      than spent - so the position contributes only its unrealized P&L.
    """
    sign = Decimal(1) if entry_side is Side.BUY else Decimal(-1)
    if market_type is MarketType.SPOT:
        return sign * executable_price * quantity
    return sign * (executable_price - entry_price) * quantity
