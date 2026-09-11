"""Terminal view of the monitored markets - what ``make market-data`` prints."""

from __future__ import annotations

import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from trading_bot.db.models.enums import MarketType
from trading_bot.marketdata.models import BookLiquidity, EngineHealth, MarketDataEvent
from trading_bot.monitoring.metrics import MarketMetrics, MonitorSummary

CLEAR_SCREEN = "\x1b[2J\x1b[H"
# Lines a frame uses besides market rows: header, column titles, events, footer.
FRAME_OVERHEAD = 17
_MISSING = "-"
_RECENT_EVENTS = 5
_MAGNITUDES = ((Decimal(10) ** 9, "B"), (Decimal(10) ** 6, "M"), (Decimal(10) ** 3, "K"))


@dataclass(frozen=True, slots=True)
class FrameContext:
    """What the header states besides the metrics themselves."""

    venue: str
    universe: str
    clock_skew_ms: int | None
    persistence: str
    band_bps: Decimal
    reference_notional: Decimal


def display_width(text: str) -> int:
    """Terminal columns taken by ``text``: CJK characters (Binance lists some) take two."""
    return sum(2 if unicodedata.east_asian_width(char) in ("W", "F") else 1 for char in text)


def compact(value: Decimal | None) -> str:
    """Magnitude rather than cents: 1234567 -> 1.23M."""
    if value is None:
        return _MISSING
    for threshold, suffix in _MAGNITUDES:
        if abs(value) >= threshold:
            return f"{value / threshold:.2f}{suffix}"
    return f"{value:.0f}"


def _pad(text: str, width: int, align: str) -> str:
    gap = " " * max(0, width - display_width(text))
    return text + gap if align == "<" else gap + text


def _plain(value: Decimal | None) -> str:
    # normalize() drops Binance's zero padding: "77280.00000000" -> "77280".
    return _MISSING if value is None else f"{value.normalize():f}"


def _fixed(value: Decimal | None, places: int) -> str:
    return _MISSING if value is None else f"{value:.{places}f}"


def _signed(value: Decimal | None) -> str:
    return _MISSING if value is None else f"{value:+.2f}"


def _side(value: Decimal, complete: bool) -> str:
    # A lower bound: the band reached past the price range the snapshot covered.
    return compact(value) if complete else f"≥{compact(value)}"


def _slippage(liquidity: BookLiquidity | None) -> str:
    if liquidity is None:
        return _MISSING
    parts = (liquidity.buy_slippage_bps, liquidity.sell_slippage_bps)
    return "/".join("n/a" if part is None else f"{part:.1f}" for part in parts)


def _latency(metrics: MarketMetrics) -> str:
    if metrics.latency_p50_ms is not None and metrics.latency_p95_ms is not None:
        return f"{metrics.latency_p50_ms}/{metrics.latency_p95_ms}"
    return _MISSING if metrics.latency_ms is None else str(metrics.latency_ms)


def _band(context: FrameContext) -> str:
    return f"±{context.band_bps.normalize():f}bp"


def _columns(context: FrameContext) -> tuple[tuple[str, int, str], ...]:
    """(title, width, alignment) per column."""
    return (
        ("#", 3, ">"),
        ("MARKET", 18, "<"),
        ("STATUS", 12, "<"),
        ("MID", 14, ">"),
        ("SPREAD bps", 10, ">"),
        ("SPREAD %", 8, ">"),
        ("AVG bps", 8, ">"),
        ("VOL 24h", 8, ">"),
        ("IMBAL", 6, ">"),
        (f"BID {_band(context)}", 11, ">"),
        (f"ASK {_band(context)}", 11, ">"),
        (f"SLIP {compact(context.reference_notional)}", 12, ">"),
        ("LAT p50/95", 10, ">"),
        ("AGE ms", 7, ">"),
    )


def _cells(metrics: MarketMetrics) -> list[str]:
    liquidity = metrics.liquidity
    kind = "spot" if metrics.ref.market_type is MarketType.SPOT else "perp"
    return [
        _MISSING if metrics.rank is None else str(metrics.rank),
        f"{metrics.ref.symbol} {kind}",
        metrics.status.value,
        _plain(metrics.mid_price),
        _fixed(metrics.spread_bps, 2),
        _fixed(metrics.spread_pct, 4),
        _fixed(metrics.spread_bps_mean, 2),
        compact(metrics.quote_volume_24h),
        _signed(metrics.imbalance),
        _MISSING if liquidity is None else _side(liquidity.bid_notional, liquidity.bid_complete),
        _MISSING if liquidity is None else _side(liquidity.ask_notional, liquidity.ask_complete),
        _slippage(liquidity),
        _latency(metrics),
        _MISSING if metrics.age_ms is None else str(metrics.age_ms),
    ]


def _row(cells: Sequence[str], columns: Sequence[tuple[str, int, str]]) -> str:
    return "  ".join(
        _pad(cell, width, align) for cell, (_, width, align) in zip(cells, columns, strict=True)
    )


def render(
    metrics: Sequence[MarketMetrics],
    summary: MonitorSummary,
    health: EngineHealth,
    events: Sequence[MarketDataEvent],
    *,
    now: datetime,
    context: FrameContext,
    max_rows: int | None = None,
) -> str:
    columns = _columns(context)
    skew = "unknown" if context.clock_skew_ms is None else f"{context.clock_skew_ms:+d} ms"
    lines = [
        f"{context.venue.upper()} market monitor  {now:%Y-%m-%d %H:%M:%S} UTC   {context.universe}",
        f"markets {summary.markets}   fresh {summary.fresh}   stale {summary.stale}   "
        f"disconnected {summary.disconnected}   "
        f"connections {health.connections_up}/{health.connections_total}   "
        f"books {health.books_synced}/{health.books_total} synced   "
        f"invalid messages {health.invalid_messages}",
        f"median spread {_fixed(summary.median_spread_bps, 2)} bps   "
        f"24h volume {compact(summary.quote_volume_24h)}   clock skew {skew} (in latency)   "
        f"persistence: {context.persistence}",
        f"liquidity = value resting within {_band(context)} of mid, from the full local book "
        f"(≥ = lower bound)   slippage = buy/sell bps for a "
        f"{compact(context.reference_notional)} market order",
        "",
    ]
    header = _row([title for title, _, _ in columns], columns)
    lines += [header, "-" * display_width(header)]
    rows = list(metrics)
    shown = rows if max_rows is None else rows[: max(0, max_rows)]
    lines += [_row(_cells(market), columns) for market in shown]
    if len(shown) < len(rows):
        lines.append(
            f"... {len(rows) - len(shown)} more markets - enlarge the terminal to see them"
        )
    if events:
        lines += ["", "Recent events"]
        lines += [
            f"  {event.occurred_at:%H:%M:%S}  {event.severity.value:<7}  {event.message}"
            for event in list(events)[-_RECENT_EVENTS:]
        ]
    lines += ["", "Ctrl-C to stop."]
    return "\n".join(lines)
