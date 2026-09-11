"""Local order-book synchronisation.

The two recorded sequences are real - a REST snapshot plus the diff stream
around it, from spot and from USD-M futures - and they follow different rules:
spot update ids are contiguous, futures ids jump and chain on ``pu``. Passing
both is the evidence that the book follows each venue's actual rule.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from itertools import pairwise
from pathlib import Path

import orjson
import pytest

from trading_bot.db.models.enums import MarketType
from trading_bot.exchange.binance.mapping import parse_order_book
from trading_bot.exchange.binance.streams import BinanceStreamSource
from trading_bot.exchange.models import BookLevel, DepthDiff, MarketRef, OrderBook
from trading_bot.exchange.streaming import StreamEndpoint, StreamKind
from trading_bot.marketdata.order_book import (
    BookSyncError,
    DepthExhaustedError,
    LocalOrderBook,
    SequenceGapError,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "binance"
SPOT = MarketRef("binance", "BTCUSDT", MarketType.SPOT)
PERP = MarketRef("binance", "BTCUSDT", MarketType.PERPETUAL)
T0 = datetime(2026, 9, 11, 7, 40, tzinfo=UTC)


def recorded(ref: MarketRef) -> tuple[OrderBook, list[DepthDiff]]:
    spot = ref.market_type is MarketType.SPOT
    name = "ws_spot_depth_sequence" if spot else "ws_futures_depth_sequence"
    data = json.loads((FIXTURES / f"{name}.json").read_text())
    snapshot = parse_order_book(data["snapshot"], ref, local_timestamp=T0)
    endpoint = StreamEndpoint(
        name="test",
        url="wss://test",
        market_type=ref.market_type,
        streams=((ref, StreamKind.DEPTH),),
    )
    source = BinanceStreamSource()
    diffs: list[DepthDiff] = []
    for message in data["diffs"]:
        diff = source.parse(endpoint, orjson.dumps(message), T0)
        assert isinstance(diff, DepthDiff)
        diffs.append(diff)
    return snapshot, diffs


def ladder(sequence: int | None = 100, levels: int = 5) -> OrderBook:
    """Bids 99, 98, ... and asks 101, 102, ... with one unit on each level."""
    return OrderBook(
        ref=SPOT,
        bids=tuple(BookLevel(Decimal(100 - i), Decimal(1)) for i in range(1, levels + 1)),
        asks=tuple(BookLevel(Decimal(100 + i), Decimal(1)) for i in range(1, levels + 1)),
        local_timestamp=T0,
        sequence=sequence,
    )


def diff(
    first: int,
    final: int,
    *,
    bids: tuple[tuple[str, str], ...] = (),
    asks: tuple[tuple[str, str], ...] = (),
    previous: int | None = None,
) -> DepthDiff:
    return DepthDiff(
        ref=SPOT,
        first_update_id=first,
        final_update_id=final,
        bids=tuple(BookLevel(Decimal(price), Decimal(size)) for price, size in bids),
        asks=tuple(BookLevel(Decimal(price), Decimal(size)) for price, size in asks),
        local_timestamp=T0,
        previous_final_update_id=previous,
    )


def loaded(snapshot: OrderBook | None = None, *, min_levels: int = 1) -> LocalOrderBook:
    snapshot = snapshot or ladder()
    book = LocalOrderBook(snapshot.ref, min_levels=min_levels)
    book.load_snapshot(snapshot)
    return book


class TestRecordedSequences:
    @pytest.mark.parametrize("ref", [SPOT, PERP], ids=["spot", "futures"])
    def test_synchronises_from_real_data(self, ref: MarketRef) -> None:
        snapshot, diffs = recorded(ref)
        book = loaded(snapshot, min_levels=20)
        applied = [book.apply(d) for d in diffs]
        # Three updates predate the snapshot; the rest bridge it and follow on.
        assert applied == [False] * 3 + [True] * (len(diffs) - 3)
        assert book.last_update_id == diffs[-1].final_update_id
        top = book.top(20)
        assert top.best_bid < top.best_ask
        assert top.sequence == diffs[-1].final_update_id

    @pytest.mark.parametrize("ref", [SPOT, PERP], ids=["spot", "futures"])
    def test_a_lost_update_is_detected(self, ref: MarketRef) -> None:
        snapshot, diffs = recorded(ref)
        book = loaded(snapshot, min_levels=20)
        for d in diffs[:6]:
            book.apply(d)
        with pytest.raises(SequenceGapError):
            book.apply(diffs[7])  # diffs[6] never arrived

    def test_spot_and_futures_chain_ids_differently(self) -> None:
        """Why one continuity rule cannot serve both venues."""
        _, spot = recorded(SPOT)
        _, futures = recorded(PERP)
        assert all(b.first_update_id == a.final_update_id + 1 for a, b in pairwise(spot))
        assert not any(b.first_update_id == a.final_update_id + 1 for a, b in pairwise(futures))
        assert all(b.previous_final_update_id == a.final_update_id for a, b in pairwise(futures))


class TestSequencing:
    def test_first_update_must_straddle_the_snapshot(self) -> None:
        with pytest.raises(SequenceGapError, match="snapshot ends at 100"):
            loaded().apply(diff(102, 103))

    def test_updates_already_in_the_snapshot_are_ignored(self) -> None:
        book = loaded()
        assert book.apply(diff(90, 99, bids=(("99", "5"),))) is False
        assert book.top(1).bids[0].size == 1

    def test_update_ending_at_the_snapshot_reapplies_harmlessly(self) -> None:
        book = loaded()
        assert book.apply(diff(95, 100, bids=(("99", "1"),))) is True
        assert book.apply(diff(101, 101)) is True

    def test_duplicates_after_bridging_are_ignored(self) -> None:
        book = loaded()
        assert book.apply(diff(101, 101)) is True
        assert book.apply(diff(101, 101)) is False

    def test_futures_style_updates_chain_on_the_previous_id(self) -> None:
        book = loaded()
        book.apply(diff(95, 105))
        assert book.apply(diff(110, 120, previous=105)) is True
        with pytest.raises(SequenceGapError):
            book.apply(diff(130, 140, previous=125))

    def test_apply_before_a_snapshot_raises(self) -> None:
        with pytest.raises(BookSyncError, match="no snapshot"):
            LocalOrderBook(SPOT, min_levels=1).apply(diff(1, 1))

    def test_snapshot_without_an_update_id_is_useless(self) -> None:
        with pytest.raises(BookSyncError, match="no update id"):
            LocalOrderBook(SPOT, min_levels=1).load_snapshot(ladder(sequence=None))

    def test_snapshot_for_another_market_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="loaded into book"):
            LocalOrderBook(PERP, min_levels=1).load_snapshot(ladder())

    def test_reset_forgets_the_book(self) -> None:
        book = loaded()
        book.reset()
        assert not book.is_loaded
        with pytest.raises(BookSyncError):
            book.top(1)

    def test_min_levels_must_be_positive(self) -> None:
        with pytest.raises(ValueError, match="min_levels"):
            LocalOrderBook(SPOT, min_levels=0)


class TestLevels:
    def test_size_zero_removes_a_level(self) -> None:
        book = loaded()
        book.apply(diff(101, 101, bids=(("99", "0"),)))
        assert book.top(1).best_bid == Decimal(98)

    def test_new_levels_inside_the_known_range_are_added(self) -> None:
        book = loaded()
        book.apply(diff(101, 101, bids=(("99.5", "2"),)))
        assert book.top(1).bids[0] == BookLevel(Decimal("99.5"), Decimal(2))

    def test_levels_beyond_the_known_range_are_not_invented(self) -> None:
        """The snapshot never saw prices below 95, so a lone update there is not depth."""
        book = loaded()
        book.apply(diff(101, 101, bids=(("50", "7"),)))
        with pytest.raises(DepthExhaustedError):
            book.top(6)

    def test_market_moving_past_the_range_demands_a_rebuild(self) -> None:
        book = loaded(ladder(levels=3), min_levels=2)
        with pytest.raises(DepthExhaustedError, match="fewer than 2"):
            book.apply(diff(101, 101, asks=(("101", "0"), ("102", "0"))))

    def test_crossed_local_book_is_reported_on_read(self) -> None:
        book = loaded()
        assert book.apply(diff(101, 101, bids=(("101", "1"),))) is True
        with pytest.raises(BookSyncError, match="crossed"):
            book.top(1)

    def test_top_is_immutable_and_carries_the_update_id(self) -> None:
        book = loaded()
        book.apply(diff(101, 101))
        top = book.top(3)
        assert top.sequence == 101
        assert top.bids == tuple(BookLevel(Decimal(p), Decimal(1)) for p in (99, 98, 97))
        assert top.asks == tuple(BookLevel(Decimal(p), Decimal(1)) for p in (101, 102, 103))
