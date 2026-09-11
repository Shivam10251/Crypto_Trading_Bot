"""Exchange and Market Data health, judged by what the market-data service wrote.

The service runs as a separate process, so the API cannot ask it directly. It
looks at the evidence instead: a monitored market counts as live when its
newest stored quote falls inside the freshness window. Nothing is inferred from
configuration, and an unreadable database reads as OFFLINE, never as a guess.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import func, select

from trading_bot.api.schemas import ComponentHealth, ComponentStatus
from trading_bot.core.config import Settings
from trading_bot.core.logging import get_logger
from trading_bot.db.models import Market, MarketData
from trading_bot.db.models.enums import MarketType
from trading_bot.db.session import get_session_factory

logger = get_logger(__name__)

START_HINT = "start it with `make market-data`"


def monitored_markets(settings: Settings) -> list[tuple[str, MarketType]]:
    return [(symbol, MarketType.SPOT) for symbol in settings.markets.spot_symbols] + [
        (symbol, MarketType.PERPETUAL) for symbol in settings.markets.perpetual_symbols
    ]


async def newest_quote_times(venue: str, since: datetime) -> dict[tuple[str, MarketType], datetime]:
    """Newest stored quote per market, looking back no further than ``since``.

    Bounding the scan by time lets the query use the local-timestamp index
    rather than aggregating days of rows.
    """
    statement = (
        select(Market.symbol, Market.market_type, func.max(MarketData.local_timestamp))
        .join(MarketData, MarketData.market_id == Market.id)
        .where(Market.venue == venue, MarketData.local_timestamp >= since)
        .group_by(Market.symbol, Market.market_type)
    )
    async with get_session_factory()() as session:
        result = await session.execute(statement)
        return {(symbol, kind): newest for symbol, kind, newest in result.all()}


async def market_data_components(
    settings: Settings, now: datetime
) -> tuple[ComponentHealth, ComponentHealth]:
    """The (Exchange, Market Data) rows of the system-status panel."""
    venue = settings.exchange.venue
    window = timedelta(milliseconds=settings.market_data.status_fresh_within_ms)
    wanted = monitored_markets(settings)
    try:
        newest = await newest_quote_times(venue, now - window)
    except Exception as exc:  # a status endpoint must never raise
        logger.warning("status.market_data_unreadable", error=str(exc))
        detail = "cannot read market data from the database"
        return (
            ComponentHealth(name="Exchange", status=ComponentStatus.OFFLINE, detail=detail),
            ComponentHealth(name="Market Data", status=ComponentStatus.OFFLINE, detail=detail),
        )

    fresh = [market for market in wanted if market in newest]
    if not fresh:
        return (
            ComponentHealth(
                name="Exchange",
                status=ComponentStatus.OFFLINE,
                detail=f"no live connection to {venue} (market-data service not running)",
            ),
            ComponentHealth(
                name="Market Data",
                status=ComponentStatus.OFFLINE,
                detail=f"no quotes in the last {window.total_seconds():g}s - {START_HINT}",
            ),
        )

    exchange = ComponentHealth(
        name="Exchange",
        status=ComponentStatus.HEALTHY,
        detail=f"streaming {venue} via the market-data service",
    )
    if len(fresh) == len(wanted):
        age_ms = max(0, int((now - max(newest[m] for m in fresh)).total_seconds() * 1000))
        return exchange, ComponentHealth(
            name="Market Data",
            status=ComponentStatus.HEALTHY,
            detail=f"{len(fresh)}/{len(wanted)} markets live, newest quote {age_ms} ms ago",
        )
    quiet = ", ".join(
        f"{symbol} {kind.value.lower()}" for symbol, kind in wanted if (symbol, kind) not in newest
    )
    return exchange, ComponentHealth(
        name="Market Data",
        status=ComponentStatus.DEGRADED,
        detail=f"{len(fresh)}/{len(wanted)} markets live; no recent quotes for {quiet}",
    )
