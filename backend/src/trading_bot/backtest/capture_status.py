"""What replay capture has actually recorded, read from the database.

The recorder's own counters (``CaptureHealth``) die with its process. Whether
a window can be replayed is a property of the rows, so this reads the rows:
per market, the newest quote, book and funding observation and how many of
each landed in the window, and every gap the recorder reported. It is what
``trading-bot-backtest capture`` prints, from any process, at any time.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, select

from trading_bot.backtest.loop import SessionFactory
from trading_bot.db.models import (
    FundingObservation,
    Market,
    MarketData,
    OrderBookSnapshot,
    SystemEvent,
)
from trading_bot.db.models.enums import MarketType, SystemEventType
from trading_bot.marketdata.capture import CAPTURE_COMPONENT


@dataclass(frozen=True, slots=True)
class StreamStatus:
    rows: int
    latest: datetime | None


@dataclass(frozen=True, slots=True)
class MarketCapture:
    market: str
    quotes: StreamStatus
    books: StreamStatus
    funding: StreamStatus | None


@dataclass(frozen=True, slots=True)
class CaptureStatus:
    since: datetime
    until: datetime
    markets: list[MarketCapture]
    gaps: list[str]


async def capture_status(
    session_factory: SessionFactory, *, venue: str, until: datetime, window: timedelta
) -> CaptureStatus:
    since = until - window
    async with session_factory() as session:
        markets = (
            await session.execute(
                select(Market.id, Market.symbol, Market.market_type)
                .where(Market.venue == venue, Market.is_monitored.is_(True))
                .order_by(Market.symbol, Market.market_type)
            )
        ).all()
        rows: list[MarketCapture] = []
        for market_id, symbol, market_type in markets:
            streams: list[StreamStatus] = []
            for table in (MarketData, OrderBookSnapshot, FundingObservation):
                count, latest = (
                    await session.execute(
                        select(func.count(table.id), func.max(table.local_timestamp)).where(
                            table.market_id == market_id,
                            table.local_timestamp >= since,
                            table.local_timestamp < until,
                        )
                    )
                ).one()
                streams.append(StreamStatus(int(count), latest))
            rows.append(
                MarketCapture(
                    market=f"{venue}:{symbol}:{market_type.value}",
                    quotes=streams[0],
                    books=streams[1],
                    funding=streams[2] if market_type is not MarketType.SPOT else None,
                )
            )
        gaps = (
            await session.execute(
                select(SystemEvent.message)
                .where(
                    SystemEvent.component == CAPTURE_COMPONENT,
                    SystemEvent.event_type == SystemEventType.DATA_GAP,
                    SystemEvent.occurred_at >= since,
                    SystemEvent.occurred_at < until,
                )
                .order_by(SystemEvent.occurred_at)
            )
        ).scalars()
        return CaptureStatus(since=since, until=until, markets=rows, gaps=list(gaps))


def render_capture(status: CaptureStatus) -> str:
    def stream(value: StreamStatus | None) -> str:
        if value is None:
            return "-"
        latest = value.latest.isoformat(timespec="seconds") if value.latest else "never"
        return f"{value.rows} (last {latest})"

    lines = [
        f"Replay capture {status.since.isoformat()} -> {status.until.isoformat()}",
        f"  {'market':<32} {'quotes':<36} {'books':<36} funding",
    ]
    lines += [
        f"  {row.market:<32} {stream(row.quotes):<36} {stream(row.books):<36} {stream(row.funding)}"
        for row in status.markets
    ] or ["  no monitored markets"]
    lines.append(f"  recorded gaps: {len(status.gaps)}")
    lines += [f"    {gap}" for gap in status.gaps]
    return "\n".join(lines)
