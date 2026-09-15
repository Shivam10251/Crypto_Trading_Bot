"""The replayed market view: nothing from after now, and nothing carried forever."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from trading_bot.backtest.clock import ReplayClock, ReplayInvariantError
from trading_bot.backtest.events import EventKind, ReplayEvent
from trading_bot.backtest.market_state import CarryLimits, ReplayMarketData
from trading_bot.backtest.source import DatasetIssues
from trading_bot.db.models.enums import MarketType
from trading_bot.exchange.models import BookLevel, FundingInfo, MarketRef, OrderBook, Quote
from trading_bot.marketdata.models import BookStatus, FeedStatus

T0 = datetime(2026, 9, 1, 7, 59, tzinfo=UTC)
SPOT = MarketRef("replay", "BTCUSDT", MarketType.SPOT)
PERP = MarketRef("replay", "BTCUSDT", MarketType.PERPETUAL)
LIMITS = CarryLimits(
    depth_levels=2,
    market_silence_ms=30_000,
    max_book_carry_ms=5_000,
    max_funding_carry_ms=60_000,
    liquidity_band_bps=Decimal(10),
    reference_notional=Decimal(1_000),
)
_ids = iter(range(1, 10_000))


def at(ms: int) -> datetime:
    return T0 + timedelta(milliseconds=ms)


def quote_event(ms: int, bid: str, ask: str, *, exchange_ms: int | None = None) -> ReplayEvent:
    quote = Quote(
        ref=SPOT,
        bid=Decimal(bid),
        ask=Decimal(ask),
        bid_size=Decimal(1),
        ask_size=Decimal(1),
        local_timestamp=at(ms),
        exchange_timestamp=at(exchange_ms) if exchange_ms is not None else None,
        sequence=ms + 1,
    )
    return ReplayEvent(EventKind.QUOTE, SPOT, at(ms), next(_ids), quote, sequence=ms + 1)


def book_event(
    ms: int, bid: str, ask: str, *, levels: int = 3, sequence: int | None = None
) -> ReplayEvent:
    book = OrderBook(
        ref=SPOT,
        bids=tuple(BookLevel(Decimal(bid) - i, Decimal(1)) for i in range(levels)),
        asks=tuple(BookLevel(Decimal(ask) + i, Decimal(1)) for i in range(levels)),
        local_timestamp=at(ms),
        sequence=sequence or ms + 1,
        bids_complete=False,
        asks_complete=False,
    )
    return ReplayEvent(EventKind.BOOK, SPOT, at(ms), next(_ids), book, sequence=book.sequence)


def funding_event(ms: int, *, settles_at: datetime, rate: str = "0.0001") -> ReplayEvent:
    funding = FundingInfo(
        ref=PERP,
        mark_price=Decimal(100_000),
        index_price=Decimal(100_000),
        last_funding_rate=Decimal(rate),
        next_funding_time=settles_at,
        local_timestamp=at(ms),
        funding_interval_hours=8,
    )
    return ReplayEvent(EventKind.FUNDING, PERP, at(ms), next(_ids), funding)


def replay(
    *events: ReplayEvent, exhausted: bool = True
) -> tuple[ReplayClock, ReplayMarketData, DatasetIssues]:
    clock = ReplayClock(T0)
    issues = DatasetIssues()
    market = ReplayMarketData((SPOT, PERP), clock, LIMITS, issues)
    market.extend(events)
    if exhausted:
        market.mark_exhausted()
    return clock, market, issues


class TestNoLookAhead:
    def test_an_event_one_microsecond_ahead_is_invisible(self) -> None:
        clock, market, _ = replay(
            quote_event(0, "100", "101"),
            ReplayEvent(
                EventKind.QUOTE,
                SPOT,
                T0 + timedelta(microseconds=1),
                next(_ids),
                Quote(
                    SPOT,
                    Decimal(500),
                    Decimal(501),
                    Decimal(1),
                    Decimal(1),
                    T0 + timedelta(microseconds=1),
                ),
            ),
        )
        assert market.snapshot(SPOT).best_bid == Decimal(100)
        clock.advance_to(T0 + timedelta(microseconds=1))
        assert market.snapshot(SPOT).best_bid == Decimal(500)

    def test_availability_is_local_receipt_not_the_exchange_clock(self) -> None:
        """The venue stamped it earlier; we could not have seen it until we received it."""
        clock, market, _ = replay(
            quote_event(0, "100", "101"), quote_event(80, "90", "91", exchange_ms=10)
        )
        clock.advance_to(at(50))
        assert market.snapshot(SPOT).best_bid == Decimal(100)
        clock.advance_to(at(80))
        assert market.snapshot(SPOT).best_bid == Decimal(90)
        assert market.snapshot(SPOT).latency_ms == 70

    def test_a_read_beyond_what_is_buffered_is_refused(self) -> None:
        """Answering from a partial buffer would hide events that had arrived."""
        clock, market, _ = replay(quote_event(0, "100", "101"), exhausted=False)
        clock.advance_to(at(10))
        with pytest.raises(ReplayInvariantError, match="lookahead"):
            market.snapshot(SPOT)

    def test_an_event_exactly_at_now_is_visible_only_when_later_ones_are_buffered(self) -> None:
        _, market, _ = replay(
            quote_event(0, "100", "101"), quote_event(10, "95", "96"), exhausted=False
        )
        assert market.covers(at(9))
        assert not market.covers(at(10)), "more events at exactly 10 ms may still be unread"

    def test_events_must_arrive_in_replay_order(self) -> None:
        _, market, _ = replay(exhausted=False)
        market.extend([quote_event(10, "100", "101")])
        with pytest.raises(ReplayInvariantError):
            market.extend([quote_event(5, "100", "101")])


class TestBooks:
    def test_the_published_book_is_capped_and_the_execution_book_is_not(self) -> None:
        _, market, _ = replay(book_event(0, "100", "101", levels=4), quote_event(0, "100", "101"))
        snapshot = market.snapshot(SPOT)
        assert snapshot.book_status is BookStatus.SYNCED
        assert snapshot.book is not None and len(snapshot.book.bids) == LIMITS.depth_levels
        execution = market.execution_snapshot(SPOT).book
        assert execution is not None and len(execution.bids) == 4
        assert not execution.bids_complete, "a capped recording never claims complete depth"

    def test_a_book_ages_between_samples_and_is_withdrawn_past_the_carry_bound(self) -> None:
        clock, market, issues = replay(book_event(0, "100", "101"), quote_event(0, "100", "101"))
        clock.advance_to(at(4_000))
        assert market.snapshot(SPOT).book_age_ms == 4_000
        clock.advance_to(at(5_001))
        snapshot = market.snapshot(SPOT)
        assert snapshot.book is None
        assert snapshot.book_status is BookStatus.SYNCING
        assert issues.counts["book_carry_expired"] == 1

    def test_a_silent_market_reads_stale(self) -> None:
        clock, market, _ = replay(quote_event(0, "100", "101"))
        clock.advance_to(at(30_001))
        assert market.snapshot(SPOT).status is FeedStatus.STALE

    def test_nothing_received_reads_connecting(self) -> None:
        _, market, _ = replay()
        snapshot = market.snapshot(SPOT)
        assert snapshot.status is FeedStatus.CONNECTING
        assert snapshot.quote is None and snapshot.book is None


class TestFunding:
    def test_funding_is_forgotten_past_its_carry_bound(self) -> None:
        settles = T0 + timedelta(minutes=1)
        clock, market, issues = replay(funding_event(0, settles_at=settles))
        assert PERP in market.rates
        clock.advance_to(at(60_001))
        assert PERP not in market.rates
        assert issues.counts["funding_carry_expired"] == 1

    def test_the_last_observation_before_a_settlement_is_kept_for_it(self) -> None:
        settles = T0 + timedelta(seconds=90)
        later = settles + timedelta(hours=8)
        clock, market, _ = replay(
            funding_event(0, settles_at=settles, rate="0.0001"),
            funding_event(60_000, settles_at=settles, rate="0.0003"),
            funding_event(120_000, settles_at=later, rate="-0.0002"),
        )
        clock.advance_to(at(120_000))
        observations = market.settlement_observations(PERP)
        assert observations[settles].rate == Decimal("0.0003")
        assert observations[settles].observed_at == at(60_000)
        assert observations[later].rate == Decimal("-0.0002")

    def test_a_settlement_observed_only_in_the_future_is_not_known_yet(self) -> None:
        settles = T0 + timedelta(seconds=90)
        clock, market, _ = replay(funding_event(60_000, settles_at=settles))
        clock.advance_to(at(59_999))
        assert market.settlement_observations(PERP) == {}
