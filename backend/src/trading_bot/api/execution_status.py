"""Paper Execution health, judged by what it wrote.

Same problem and same answer as the market feed and the strategy: the
simulator runs inside the market-data process, so the API cannot ask it
anything and reads its output instead.

The honesty rule that matters here is the inverse of the strategy's. A
strategy recording nothing but rejections is working correctly; an execution
engine that has *never filled anything* is also working correctly, because
the strategy has validated nothing to fill. So "no orders" is only reported
as a fault when execution is switched on and something should have reached
it - and even then it is DEGRADED with the reason, never a silent HEALTHY.

Shadow probes are counted separately and never folded into the totals. They
are measurements of the venue, not trades the strategy asked for.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import func, select

from trading_bot.api.schemas import ComponentHealth, ComponentStatus
from trading_bot.core.config import Settings
from trading_bot.core.logging import get_logger
from trading_bot.db.models import Order
from trading_bot.db.models.enums import ExecutionMode, OrderStatus
from trading_bot.db.session import get_session_factory

logger = get_logger(__name__)

NAME = "Paper Execution"
START_HINT = "start it with `make market-data`"
# Generous: an opportunity that clears validation is rare by design, so this
# detects a dead simulator rather than a quiet market.
STALE_AFTER = timedelta(minutes=30)


async def recent_orders(since: datetime) -> tuple[int, int, int, datetime | None]:
    """(orders, of those filled, of those shadow probes, newest) since ``since``."""
    statement = select(
        func.count(Order.id).filter(Order.is_shadow.is_(False)),
        func.count(Order.id).filter(Order.status == OrderStatus.FILLED, Order.is_shadow.is_(False)),
        func.count(Order.id).filter(Order.is_shadow.is_(True)),
        func.max(Order.created_at),
    ).where(Order.created_at >= since, Order.mode == ExecutionMode.PAPER)
    async with get_session_factory()() as session:
        total, filled, shadow, newest = (await session.execute(statement)).one()
        return int(total or 0), int(filled or 0), int(shadow or 0), newest


async def execution_status(settings: Settings, now: datetime) -> ComponentHealth:
    """The Paper Execution row of the system-status panel."""
    config = settings.execution
    if not (config.enabled or config.shadow):
        return ComponentHealth(
            name=NAME,
            status=ComponentStatus.OFFLINE,
            detail="paper execution is switched off in configuration",
        )
    try:
        total, filled, shadow, newest = await recent_orders(now - STALE_AFTER)
    except Exception as exc:  # a status endpoint must never raise
        logger.warning("status.execution_unreadable", error=str(exc))
        return ComponentHealth(
            name=NAME,
            status=ComponentStatus.OFFLINE,
            detail="cannot read orders from the database",
        )

    window = int(STALE_AFTER.total_seconds() // 60)
    if newest is None:
        # Nothing to fill is the normal state of this strategy, so this is not
        # a failure - but it is also not evidence the simulator is alive, and
        # saying HEALTHY on no evidence is what this codebase refuses to do.
        return ComponentHealth(
            name=NAME,
            status=ComponentStatus.OFFLINE,
            detail=(
                f"armed ({config.mode.value.lower()}), but no orders in {window} min - "
                "the strategy has validated nothing to execute"
            ),
        )
    probes = f", {shadow} shadow probes" if shadow else ""
    return ComponentHealth(
        name=NAME,
        status=ComponentStatus.HEALTHY,
        detail=f"{total} paper orders in {window} min, {filled} filled{probes}",
    )
