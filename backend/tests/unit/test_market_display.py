"""The terminal view shows what the engine knows - and dashes for what it does not."""

from __future__ import annotations

import dataclasses
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal

from trading_bot.db.models.enums import MarketType, Severity, SystemEventType
from trading_bot.exchange.models import BookLevel, MarketRef, OrderBook, Quote
from trading_bot.marketdata.display import render
from trading_bot.marketdata.models import (
    BookStatus,
    EngineHealth,
    FeedStatus,
    MarketDataEvent,
    MarketSnapshot,
)

NOW = datetime(2026, 9, 11, 7, 40, tzinfo=UTC)
SPOT = MarketRef("binance", "BTCUSDT", MarketType.SPOT)
PERP = MarketRef("binance", "BTCUSDT", MarketType.PERPETUAL)
HEALTH = EngineHealth(
    connections_up=1,
    connections_total=2,
    markets_live=1,
    markets_total=2,
    books_synced=0,
    books_total=2,
    invalid_messages=3,
)


def live(ref: MarketRef) -> MarketSnapshot:
    quote = Quote(
        ref=ref,
        bid=Decimal("77280.00000000"),
        ask=Decimal("77280.01000000"),
        bid_size=Decimal("0.96065000"),
        ask_size=Decimal("5.90705000"),
        local_timestamp=NOW,
    )
    return MarketSnapshot(
        ref=ref,
        status=FeedStatus.LIVE,
        quote=quote,
        book=None,
        book_status=BookStatus.SYNCING,
        last_price=Decimal("77280"),
        volume_24h=Decimal("15245.41532"),
        quote_volume_24h=None,
        latency_ms=12,
        last_update_at=NOW,
        age_ms=3,
        updates=10,
        gaps=0,
        resyncs=1,
    )


def waiting(ref: MarketRef) -> MarketSnapshot:
    return MarketSnapshot(
        ref=ref,
        status=FeedStatus.CONNECTING,
        quote=None,
        book=None,
        book_status=BookStatus.SYNCING,
        last_price=None,
        volume_24h=None,
        quote_volume_24h=None,
        latency_ms=None,
        last_update_at=None,
        age_ms=None,
        updates=0,
        gaps=0,
        resyncs=0,
    )


def frame(
    snapshots: Sequence[MarketSnapshot] | None = None,
    events: Sequence[MarketDataEvent] = (),
    skew: int | None = -3,
) -> str:
    return render(
        snapshots if snapshots is not None else [live(SPOT), waiting(PERP)],
        HEALTH,
        events,
        now=NOW,
        venue="binance",
        clock_skew_ms=skew,
        persistence="market_data every 1000 ms",
    )


def row(text: str, prefix: str) -> str:
    return next(line for line in text.splitlines() if line.startswith(prefix))


class TestRender:
    def test_header_summarises_engine_health(self) -> None:
        text = frame()
        assert text.startswith("BINANCE market data  2026-09-11 07:40:00 UTC")
        for expected in ("live 1/2", "connections 1/2", "books 0/2 synced", "invalid messages 3"):
            assert expected in text
        assert "clock skew vs venue -3 ms" in text
        assert "persistence: market_data every 1000 ms" in text

    def test_live_market_shows_trimmed_prices_and_volume(self) -> None:
        line = row(frame(), "BTCUSDT spot")
        for expected in ("LIVE", "77280.01", "0.96065", "5.90705", "77280.005", "15,245.42"):
            assert expected in line
        assert "0.001" in line  # spread in bps
        assert "SYNCING" in line

    def test_a_market_without_data_shows_dashes_not_zeros(self) -> None:
        line = row(frame(), "BTCUSDT perpetual")
        assert "CONNECTING" in line
        assert " - " in line
        assert "0.00" not in line

    def test_a_synced_book_shows_its_depth(self) -> None:
        book = OrderBook(
            ref=SPOT,
            bids=(BookLevel(Decimal(99), Decimal(1)), BookLevel(Decimal(98), Decimal(1))),
            asks=(BookLevel(Decimal(101), Decimal(1)), BookLevel(Decimal(102), Decimal(1))),
            local_timestamp=NOW,
        )
        synced = dataclasses.replace(live(SPOT), book=book, book_status=BookStatus.SYNCED)
        assert "SYNCED 2" in row(frame([synced]), "BTCUSDT spot")

    def test_only_the_most_recent_events_are_listed(self) -> None:
        events = [
            MarketDataEvent(SystemEventType.WS_CONNECTED, Severity.INFO, f"event {i}", NOW)
            for i in range(8)
        ]
        text = frame(events=events)
        assert "event 7" in text
        assert "event 2" in text
        assert "event 1" not in text

    def test_unknown_clock_skew_is_said_plainly(self) -> None:
        assert "clock skew vs venue unknown" in frame(skew=None)
