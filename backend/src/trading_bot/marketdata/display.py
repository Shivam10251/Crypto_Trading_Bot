"""Terminal view of live market data - what ``make market-data`` prints."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal

from trading_bot.marketdata.models import (
    BookStatus,
    EngineHealth,
    MarketDataEvent,
    MarketSnapshot,
)

CLEAR_SCREEN = "\x1b[2J\x1b[H"
_MISSING = "-"
_RECENT_EVENTS = 6

# (header, width, alignment)
_COLUMNS: tuple[tuple[str, int, str], ...] = (
    ("MARKET", 18, "<"),
    ("STATUS", 12, "<"),
    ("BID", 14, ">"),
    ("ASK", 14, ">"),
    ("BID SIZE", 12, ">"),
    ("ASK SIZE", 12, ">"),
    ("MID", 15, ">"),
    ("SPREAD bps", 10, ">"),
    ("VOLUME 24h", 14, ">"),
    ("LATENCY ms", 10, ">"),
    ("AGE ms", 7, ">"),
    ("BOOK", 10, "<"),
)


def _plain(value: Decimal | None) -> str:
    # normalize() drops Binance's zero padding: "77280.00000000" -> "77280".
    return _MISSING if value is None else f"{value.normalize():f}"


def _fixed(value: Decimal | None, places: int) -> str:
    return _MISSING if value is None else f"{value:,.{places}f}"


def _count(value: int | None) -> str:
    return _MISSING if value is None else str(value)


def _book(snapshot: MarketSnapshot) -> str:
    if snapshot.book_status is BookStatus.SYNCED and snapshot.book is not None:
        return f"SYNCED {len(snapshot.book.bids)}"
    return snapshot.book_status.value


def _row(cells: Sequence[str]) -> str:
    return "  ".join(
        f"{cell:{align}{width}}" for cell, (_, width, align) in zip(cells, _COLUMNS, strict=True)
    )


def render(
    snapshots: Sequence[MarketSnapshot],
    health: EngineHealth,
    events: Sequence[MarketDataEvent],
    *,
    now: datetime,
    venue: str,
    clock_skew_ms: int | None,
    persistence: str,
) -> str:
    skew = f"{clock_skew_ms:+d} ms" if clock_skew_ms is not None else "unknown"
    lines = [
        f"{venue.upper()} market data  {now:%Y-%m-%d %H:%M:%S} UTC   "
        f"live {health.markets_live}/{health.markets_total}   "
        f"connections {health.connections_up}/{health.connections_total}   "
        f"books {health.books_synced}/{health.books_total} synced   "
        f"invalid messages {health.invalid_messages}",
        f"clock skew vs venue {skew} (included in latency)   persistence: {persistence}",
        "",
    ]
    header = _row([name for name, _, _ in _COLUMNS])
    lines += [header, "-" * len(header)]
    for snapshot in snapshots:
        lines.append(
            _row(
                [
                    f"{snapshot.ref.symbol} {snapshot.ref.market_type.value.lower()}",
                    snapshot.status.value,
                    _plain(snapshot.best_bid),
                    _plain(snapshot.best_ask),
                    _plain(snapshot.bid_size),
                    _plain(snapshot.ask_size),
                    _plain(snapshot.mid_price),
                    _fixed(snapshot.spread_bps, 3),
                    _fixed(snapshot.volume_24h, 2),
                    _count(snapshot.latency_ms),
                    _count(snapshot.age_ms),
                    _book(snapshot),
                ]
            )
        )
    if events:
        lines += ["", "Recent events"]
        for event in list(events)[-_RECENT_EVENTS:]:
            lines.append(
                f"  {event.occurred_at:%H:%M:%S}  {event.severity.value:<7}  {event.message}"
            )
    lines += ["", "Ctrl-C to stop."]
    return "\n".join(lines)
