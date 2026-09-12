"""Risk Engine health, judged by the decisions it wrote.

Same problem and same answer as the feed, the strategy and the simulator: the
risk engine runs inside the market-data process, so the API cannot ask it
anything and reads its output instead.

The judgement that matters here is different from the others, though. A risk
engine with nothing to decide is not evidence of anything, but a risk engine
that has *halted trading* is a live, load-bearing fact the dashboard must
show even though nothing is running - so the kill switch is read first, from
the same durable ``risk_events`` row the engine itself restores from, and a
halt is reported as DEGRADED with its reason rather than as OFFLINE.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import func, select

from trading_bot.api.schemas import ComponentHealth, ComponentStatus
from trading_bot.core.config import Settings
from trading_bot.core.logging import get_logger
from trading_bot.db.models import RiskEvent
from trading_bot.db.models.enums import ExecutionMode, RiskDecision, RiskEventType
from trading_bot.db.session import get_session_factory
from trading_bot.risk.kill_switch import parse_halted_until

logger = get_logger(__name__)

NAME = "Risk Engine"
# Matches Paper Execution's window: a decision is only written when there is
# something to decide, which on this strategy is rare by design.
STALE_AFTER = timedelta(minutes=30)


def kill_switch_decision_is_active(
    decision: RiskDecision, context: dict[str, object] | None, now: datetime
) -> bool:
    """Interpret the durable transition with the engine's timed-halt rules."""
    if decision is not RiskDecision.PAUSED:
        return False
    halted_until = parse_halted_until(context)
    return halted_until is None or now < halted_until


async def kill_switch_state(now: datetime) -> tuple[bool, str | None]:
    """(halted, reason) from the newest kill-switch decision on record.

    Ordered by id, exactly as ``KillSwitchState`` restores it - a status
    endpoint disagreeing with the engine about whether trading is halted
    would be worse than showing nothing.
    """
    statement = (
        select(RiskEvent.decision, RiskEvent.reason, RiskEvent.context)
        .where(
            RiskEvent.event_type == RiskEventType.KILL_SWITCH,
            RiskEvent.mode == ExecutionMode.PAPER,
        )
        .order_by(RiskEvent.id.desc())
        .limit(1)
    )
    async with get_session_factory()() as session:
        row = (await session.execute(statement)).first()
    if row is None:
        return False, None
    decision, reason, context = row
    return kill_switch_decision_is_active(decision, context, now), reason


async def recent_decisions(since: datetime) -> tuple[int, int, int, datetime | None]:
    """(approved, refused, shadow, newest) decisions since ``since``."""
    statement = select(
        func.count(RiskEvent.id).filter(
            RiskEvent.decision == RiskDecision.APPROVED, RiskEvent.is_shadow.is_(False)
        ),
        func.count(RiskEvent.id).filter(
            RiskEvent.decision != RiskDecision.APPROVED, RiskEvent.is_shadow.is_(False)
        ),
        func.count(RiskEvent.id).filter(RiskEvent.is_shadow.is_(True)),
        func.max(RiskEvent.occurred_at),
    ).where(RiskEvent.occurred_at >= since, RiskEvent.mode == ExecutionMode.PAPER)
    async with get_session_factory()() as session:
        approved, refused, shadow, newest = (await session.execute(statement)).one()
        return int(approved or 0), int(refused or 0), int(shadow or 0), newest


async def risk_status(settings: Settings, now: datetime) -> ComponentHealth:
    """The Risk Engine row of the system-status panel."""
    config = settings.execution
    try:
        halted, halt_reason = await kill_switch_state(now)
    except Exception as exc:  # a status endpoint must never raise
        logger.warning("status.risk_unreadable", error=str(exc))
        return ComponentHealth(
            name=NAME,
            status=ComponentStatus.OFFLINE,
            detail="cannot read risk events from the database",
        )

    if halted:
        # Worth saying loudly even when execution is switched off: a halted
        # switch is what a restarted service would restore, and re-arming it
        # is a deliberate act someone has to take.
        return ComponentHealth(
            name=NAME,
            status=ComponentStatus.DEGRADED,
            detail=f"kill switch ENGAGED: {halt_reason or 'no reason recorded'}",
        )

    if not (config.enabled or config.shadow):
        return ComponentHealth(
            name=NAME,
            status=ComponentStatus.OFFLINE,
            detail="runs with the execution service, which is switched off in configuration",
        )

    try:
        approved, refused, shadow, newest = await recent_decisions(now - STALE_AFTER)
    except Exception as exc:
        logger.warning("status.risk_unreadable", error=str(exc))
        return ComponentHealth(
            name=NAME,
            status=ComponentStatus.OFFLINE,
            detail="cannot read risk events from the database",
        )

    window = int(STALE_AFTER.total_seconds() // 60)
    if newest is None:
        # Nothing reached the gate. That is the normal state of this strategy
        # - it validates almost nothing - so it is not a fault, but it is not
        # evidence the engine is running either.
        return ComponentHealth(
            name=NAME,
            status=ComponentStatus.OFFLINE,
            detail=(
                f"armed and not halted, but no decisions in {window} min - "
                "nothing has reached the risk gate"
            ),
        )
    probes = f", {shadow} shadow" if shadow else ""
    return ComponentHealth(
        name=NAME,
        status=ComponentStatus.HEALTHY,
        detail=f"{approved} approved, {refused} refused in {window} min{probes}",
    )
