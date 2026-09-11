"""The monitoring view: what it shows, and dashes for what it does not know."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal

from trading_bot.db.models.enums import MarketType, Severity, SystemEventType
from trading_bot.exchange.models import MarketRef
from trading_bot.marketdata.models import (
    BookLiquidity,
    EngineHealth,
    FeedStatus,
    MarketDataEvent,
)
from trading_bot.monitoring.display import FrameContext, compact, display_width, render
from trading_bot.monitoring.metrics import MarketMetrics, MonitorSummary

NOW = datetime(2026, 9, 11, 9, 0, tzinfo=UTC)
BTC = MarketRef("binance", "BTCUSDT", MarketType.SPOT)
BTC_PERP = MarketRef("binance", "BTCUSDT", MarketType.PERPETUAL)
CJK = MarketRef("binance", "牛来USDT", MarketType.SPOT)
CONTEXT = FrameContext(
    venue="binance",
    universe="top 2 of 357 spot/perpetual pairs by weaker-leg 24h volume",
    clock_skew_ms=-3,
    persistence="market_data every 5000 ms",
    band_bps=Decimal(10),
    reference_notional=Decimal(10_000),
)
HEALTH = EngineHealth(
    connections_up=3,
    connections_total=3,
    markets_live=3,
    markets_total=4,
    books_synced=2,
    books_total=4,
    invalid_messages=0,
)
SUMMARY = MonitorSummary(
    markets=4,
    fresh=3,
    stale=1,
    disconnected=0,
    median_spread_bps=Decimal("1.5"),
    quote_volume_24h=Decimal("38200000000"),
    books_measured=2,
    books_complete=1,
)
BOOK = BookLiquidity(
    band_bps=Decimal(10),
    bid_notional=Decimal("7090439"),
    ask_notional=Decimal("6000000"),
    bid_complete=True,
    ask_complete=False,
    reference_notional=Decimal(10_000),
    buy_slippage_bps=Decimal("0.07"),
    sell_slippage_bps=None,
)


def live(ref: MarketRef = BTC, *, rank: int | None = 1) -> MarketMetrics:
    return MarketMetrics(
        ref=ref,
        rank=rank,
        status=FeedStatus.LIVE,
        sampled_at=NOW,
        mid_price=Decimal("77246.22500000"),
        spread=Decimal("0.01"),
        spread_bps=Decimal("0.0013"),
        spread_pct=Decimal("0.000013"),
        spread_bps_mean=Decimal("0.0021"),
        volume_24h=Decimal("15223"),
        quote_volume_24h=Decimal("1177900000"),
        liquidity=BOOK,
        imbalance=BOOK.imbalance,
        imbalance_mean=Decimal(0),
        age_ms=16,
        is_fresh=True,
        latency_ms=44,
        latency_p50_ms=41,
        latency_p95_ms=57,
        samples=60,
    )


def unknown(ref: MarketRef = BTC_PERP) -> MarketMetrics:
    return MarketMetrics(
        ref=ref,
        rank=None,
        status=FeedStatus.CONNECTING,
        sampled_at=NOW,
        mid_price=None,
        spread=None,
        spread_bps=None,
        spread_pct=None,
        spread_bps_mean=None,
        volume_24h=None,
        quote_volume_24h=None,
        liquidity=None,
        imbalance=None,
        imbalance_mean=None,
        age_ms=None,
        is_fresh=False,
        latency_ms=None,
        latency_p50_ms=None,
        latency_p95_ms=None,
        samples=0,
    )


def frame(
    metrics: Sequence[MarketMetrics] | None = None,
    events: Sequence[MarketDataEvent] = (),
    max_rows: int | None = None,
) -> str:
    return render(
        metrics if metrics is not None else [live(), unknown()],
        SUMMARY,
        HEALTH,
        events,
        now=NOW,
        context=CONTEXT,
        max_rows=max_rows,
    )


def row(text: str, marker: str) -> str:
    return next(line for line in text.splitlines() if marker in line and "  " in line)


class TestFormatting:
    def test_compact_shows_magnitude(self) -> None:
        assert compact(Decimal("1234567")) == "1.23M"
        assert compact(Decimal("2500000000")) == "2.50B"
        assert compact(Decimal("45000")) == "45.00K"
        assert compact(Decimal("999")) == "999"
        assert compact(None) == "-"

    def test_cjk_characters_take_two_columns(self) -> None:
        assert display_width("牛来USDT") == 8


class TestFrame:
    def test_header_states_the_selection_and_health(self) -> None:
        text = frame()
        assert text.startswith("BINANCE market monitor  2026-09-11 09:00:00 UTC")
        for expected in (
            CONTEXT.universe,
            "markets 4",
            "fresh 3",
            "stale 1",
            "books 2/4 synced",
            "median spread 1.50 bps",
            "24h volume 38.20B",
            "clock skew -3 ms",
            "within ±10bp of mid",
            "10.00K market order",
        ):
            assert expected in text

    def test_a_live_market_shows_every_metric(self) -> None:
        line = row(frame(), "BTCUSDT spot")
        for expected in (
            "LIVE",
            "77246.225",
            "0.00",  # spread in bps
            "1.18B",  # 24h quote volume
            "+0.08",  # imbalance
            "7.09M",
            "≥6.00M",  # the ask side ran past the snapshot: a lower bound
            "0.1/n/a",  # the sell side could not fill the reference order
            "41/57",
            "16",
        ):
            assert expected in line

    def test_a_market_without_data_shows_dashes_not_zeros(self) -> None:
        line = row(frame(), "BTCUSDT perp")
        assert "CONNECTING" in line
        assert " - " in line
        assert "0.00" not in line

    def test_rows_beyond_the_terminal_are_counted_not_silently_dropped(self) -> None:
        text = frame([live(), unknown(), live(BTC_PERP, rank=1)], max_rows=1)
        assert "... 2 more markets" in text

    def test_cjk_symbols_stay_aligned(self) -> None:
        text = frame([live(BTC), live(CJK, rank=2)])
        lines = [row(text, "BTCUSDT spot"), row(text, "牛来USDT spot")]
        columns = {display_width(line[: line.index("LIVE")]) for line in lines}
        assert len(columns) == 1

    def test_only_the_latest_events_are_listed(self) -> None:
        events = [
            MarketDataEvent(SystemEventType.WS_CONNECTED, Severity.INFO, f"event {i}", NOW)
            for i in range(8)
        ]
        text = frame(events=events)
        assert "event 7" in text
        assert "event 3" in text
        assert "event 2" not in text
