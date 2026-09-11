"""Strategy Engine health, judged by what it wrote.

The strategy runs inside the market-data process, so the API cannot ask it how
it is doing - the same problem as the market-data feed, and the same answer.
It reads the evidence: how recently an opportunity was recorded, and what the
recent ones concluded.

Detection is the deliverable here, not profit. A strategy that records nothing
but rejections is working exactly as intended, so a HEALTHY reading says how
many opportunities were seen and how many survived costs, and never treats
"nothing was tradeable" as a fault.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import func, select

from trading_bot.api.schemas import ComponentHealth, ComponentStatus
from trading_bot.core.config import Settings
from trading_bot.core.logging import get_logger
from trading_bot.db.models import Opportunity
from trading_bot.db.session import get_session_factory

logger = get_logger(__name__)

NAME = "Strategy Engine"
START_HINT = "start it with `make market-data`"
# An opportunity episode can legitimately run for minutes before it is written,
# so the window is generous: this detects a dead strategy, not a quiet market.
STALE_AFTER = timedelta(minutes=10)


async def recent_opportunities(since: datetime) -> tuple[int, int, datetime | None]:
    """(recorded, of those with a positive net edge, newest) since ``since``."""
    statement = select(
        func.count(Opportunity.id),
        func.count(Opportunity.id).filter(Opportunity.net_edge_bps > 0),
        func.max(Opportunity.detected_at),
    ).where(Opportunity.detected_at >= since)
    async with get_session_factory()() as session:
        total, positive, newest = (await session.execute(statement)).one()
        return int(total or 0), int(positive or 0), newest


async def strategy_status(settings: Settings, now: datetime) -> ComponentHealth:
    """The Strategy Engine row of the system-status panel."""
    enabled = settings.strategy.enabled
    if not enabled or not settings.strategy.evaluate:
        return ComponentHealth(
            name=NAME,
            status=ComponentStatus.OFFLINE,
            detail="no strategy enabled in configuration",
        )
    if not settings.opportunities.persist:
        # Honest about the blind spot rather than reporting a health it has no
        # evidence for: the strategy may well be running, we just cannot see it.
        return ComponentHealth(
            name=NAME,
            status=ComponentStatus.OFFLINE,
            detail="opportunity persistence is off; the API cannot observe the strategy",
        )
    try:
        total, positive, newest = await recent_opportunities(now - STALE_AFTER)
    except Exception as exc:  # a status endpoint must never raise
        logger.warning("status.strategy_unreadable", error=str(exc))
        return ComponentHealth(
            name=NAME,
            status=ComponentStatus.OFFLINE,
            detail="cannot read opportunities from the database",
        )

    names = ", ".join(enabled)
    if newest is None:
        return ComponentHealth(
            name=NAME,
            status=ComponentStatus.OFFLINE,
            detail=(
                f"no opportunities recorded in the last "
                f"{STALE_AFTER.total_seconds() / 60:g} min - {START_HINT}"
            ),
        )
    age_s = max(0, int((now - newest).total_seconds()))
    # Rejections are the expected outcome, so they are reported, not flagged.
    return ComponentHealth(
        name=NAME,
        status=ComponentStatus.HEALTHY,
        detail=(
            f"{names}: {total} opportunities recorded in the last "
            f"{STALE_AFTER.total_seconds() / 60:g} min, {positive} with a positive "
            f"net edge; newest {age_s}s ago"
        ),
    )
