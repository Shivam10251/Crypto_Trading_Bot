"""Exchange and Market Data health, judged by what the market-data service wrote.

The service runs as a separate process, so the API cannot ask it directly. It
reads the evidence instead: which markets the service last selected
(``markets.is_monitored``) and how recent each one's newest stored quote is.
Nothing is inferred from configuration - under ``top_volume`` selection the
API could not know the markets anyway - and an unreadable database reads as
OFFLINE with unknown counts, never as a guess.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import and_, func, select

from trading_bot.api.schemas import ComponentHealth, ComponentStatus
from trading_bot.core.config import Settings
from trading_bot.core.logging import get_logger
from trading_bot.db.models import Market, MarketData
from trading_bot.db.models.enums import MarketType
from trading_bot.db.session import get_session_factory

logger = get_logger(__name__)

START_HINT = "start it with `make market-data`"
# Quiet markets named in a DEGRADED detail; beyond this they are counted.
_NAMED = 5

MarketKey = tuple[str, MarketType]


@dataclass(frozen=True, slots=True)
class MarketDataStatus:
    exchange: ComponentHealth
    market_data: ComponentHealth
    # None when the database cannot say which markets are monitored.
    monitored_spot: int | None
    monitored_perpetual: int | None


async def monitored_quote_times(venue: str, since: datetime) -> dict[MarketKey, datetime | None]:
    """Every monitored market with its newest quote since ``since``, or None.

    Bounding the join by time lets it use the local-timestamp index rather
    than aggregating days of rows.
    """
    statement = (
        select(Market.symbol, Market.market_type, func.max(MarketData.local_timestamp))
        .outerjoin(
            MarketData,
            and_(MarketData.market_id == Market.id, MarketData.local_timestamp >= since),
        )
        .where(Market.venue == venue, Market.is_monitored.is_(True))
        .group_by(Market.symbol, Market.market_type)
    )
    async with get_session_factory()() as session:
        result = await session.execute(statement)
        return {(symbol, kind): newest for symbol, kind, newest in result.all()}


def _component(name: str, status: ComponentStatus, detail: str) -> ComponentHealth:
    return ComponentHealth(name=name, status=status, detail=detail)


async def market_data_status(settings: Settings, now: datetime) -> MarketDataStatus:
    """The Exchange and Market Data rows of the system-status panel."""
    venue = settings.exchange.venue
    window = timedelta(milliseconds=settings.market_data.status_fresh_within_ms)
    try:
        newest = await monitored_quote_times(venue, now - window)
    except Exception as exc:  # a status endpoint must never raise
        logger.warning("status.market_data_unreadable", error=str(exc))
        detail = "cannot read market data from the database"
        return MarketDataStatus(
            exchange=_component("Exchange", ComponentStatus.OFFLINE, detail),
            market_data=_component("Market Data", ComponentStatus.OFFLINE, detail),
            monitored_spot=None,
            monitored_perpetual=None,
        )

    spot = sum(kind is MarketType.SPOT for _, kind in newest)
    perpetual = sum(kind is MarketType.PERPETUAL for _, kind in newest)
    fresh = {key: at for key, at in newest.items() if at is not None}
    if not fresh:
        if newest:
            detail = (
                f"no quotes in the last {window.total_seconds():g}s from "
                f"{len(newest)} monitored markets - {START_HINT}"
            )
        else:
            detail = f"no markets selected yet - {START_HINT}"
        return MarketDataStatus(
            exchange=_component(
                "Exchange",
                ComponentStatus.OFFLINE,
                f"no live connection to {venue} (market-data service not running)",
            ),
            market_data=_component("Market Data", ComponentStatus.OFFLINE, detail),
            monitored_spot=spot,
            monitored_perpetual=perpetual,
        )

    exchange = _component(
        "Exchange", ComponentStatus.HEALTHY, f"streaming {venue} via the market-data service"
    )
    if len(fresh) == len(newest):
        age_ms = max(0, int((now - max(fresh.values())).total_seconds() * 1000))
        market_data = _component(
            "Market Data",
            ComponentStatus.HEALTHY,
            f"{len(fresh)}/{len(newest)} markets live, newest quote {age_ms} ms ago",
        )
    else:
        quiet = sorted(f"{symbol} {kind.value.lower()}" for symbol, kind in newest.keys() - fresh)
        named = ", ".join(quiet[:_NAMED])
        if len(quiet) > _NAMED:
            named += f" and {len(quiet) - _NAMED} more"
        market_data = _component(
            "Market Data",
            ComponentStatus.DEGRADED,
            f"{len(fresh)}/{len(newest)} markets live; no recent quotes for {named}",
        )
    return MarketDataStatus(
        exchange=exchange,
        market_data=market_data,
        monitored_spot=spot,
        monitored_perpetual=perpetual,
    )
