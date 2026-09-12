"""Portfolio & P&L health, judged by the snapshots it wrote.

Same problem and same answer as the feed, the strategy, the simulator and the
risk engine: the portfolio service runs inside the market-data process, so the
API cannot ask it anything and reads its output instead.

What counts as evidence here is a **recent durable portfolio snapshot**. Not a
configuration flag, not an open position, not an absence of errors - a row
whose ``captured_at`` is inside the expected cadence. HEALTHY without one
would be a status that could not be wrong.

Five states, and each names what is actually true:

- **OFFLINE** - switched off in configuration, or switched on and never once
  produced a snapshot. Those are different sentences, and both are said.
- **DEGRADED / stale** - the newest snapshot is older than the cadence allows,
  so the service is not keeping up or has stopped.
- **DEGRADED / valuation** - the newest snapshot could not price every open
  position (``UNAVAILABLE``). Equity is unknown; a partial total is never
  shown as account equity.
- **DEGRADED / unpaired** - a live position whose hedge no longer exists. Real
  naked exposure, and the loudest thing this component can report.
- **DEGRADED / incomplete accounting** - the newest P&L row names cash flows
  nothing could measure (funding, spot borrow), so its realised figure is a
  measurement rather than a total.
- **HEALTHY** - a recent snapshot, a complete valuation, and nothing unpaired.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select

from trading_bot.api.schemas import ComponentHealth, ComponentStatus
from trading_bot.core.config import Settings
from trading_bot.core.logging import get_logger
from trading_bot.db.models import PnlSnapshot, PortfolioSnapshot
from trading_bot.db.models.enums import ExecutionMode, ValuationStatus
from trading_bot.db.session import get_session_factory

logger = get_logger(__name__)

NAME = "Portfolio & P&L"
START_HINT = "start it with `make market-data`"
#: A snapshot older than this many intervals means the loop is not running.
STALE_INTERVALS = 3
#: Floor, so a fast cadence does not make the check flap on one slow write.
MINIMUM_STALE_AFTER = timedelta(minutes=2)


async def newest_snapshot(mode: ExecutionMode) -> PortfolioSnapshot | None:
    statement = (
        select(PortfolioSnapshot)
        .where(PortfolioSnapshot.mode == mode)
        .order_by(PortfolioSnapshot.captured_at.desc())
        .limit(1)
    )
    async with get_session_factory()() as session:
        return (await session.execute(statement)).scalars().first()


async def newest_unmeasured(mode: ExecutionMode) -> list[str] | None:
    """Components missing from the newest portfolio-scope P&L row."""
    statement = (
        select(PnlSnapshot.unmeasured_pnl)
        .where(PnlSnapshot.mode == mode, PnlSnapshot.scope_key == "portfolio")
        .order_by(PnlSnapshot.captured_at.desc())
        .limit(1)
    )
    async with get_session_factory()() as session:
        row = (await session.execute(statement)).first()
    return list(row[0]) if row is not None and row[0] else None


async def portfolio_status(settings: Settings, now: datetime) -> ComponentHealth:
    """The Portfolio & P&L row of the system-status panel."""
    config = settings.portfolio
    mode = ExecutionMode[settings.execution.mode.name]
    if not config.enabled:
        return ComponentHealth(
            name=NAME,
            status=ComponentStatus.OFFLINE,
            detail=(
                "portfolio valuation, exits and P&L are switched off in configuration; "
                "realised P&L is unavailable, so the daily-loss and consecutive-loss "
                "limits report as deferred"
            ),
        )
    try:
        snapshot = await newest_snapshot(mode)
        unmeasured = await newest_unmeasured(mode)
    except Exception as exc:  # a status endpoint must never raise
        logger.warning("status.portfolio_unreadable", error=str(exc))
        return ComponentHealth(
            name=NAME,
            status=ComponentStatus.OFFLINE,
            detail="cannot read portfolio snapshots from the database",
        )

    if snapshot is None:
        return ComponentHealth(
            name=NAME,
            status=ComponentStatus.OFFLINE,
            detail=(
                "enabled, but no portfolio snapshot has ever been written - "
                f"the portfolio service has not run ({START_HINT})"
            ),
        )

    stale_after = max(
        MINIMUM_STALE_AFTER,
        timedelta(milliseconds=config.snapshot_interval_ms * STALE_INTERVALS),
    )
    age = now - snapshot.captured_at
    exits = "exits on" if config.exits.enabled else "exits off"
    if age > stale_after:
        return ComponentHealth(
            name=NAME,
            status=ComponentStatus.DEGRADED,
            detail=(
                f"newest snapshot is {int(age.total_seconds())} s old, past the "
                f"{int(stale_after.total_seconds())} s the cadence allows - "
                "valuation and realised P&L are not being updated"
            ),
        )
    if snapshot.unpaired_positions:
        return ComponentHealth(
            name=NAME,
            status=ComponentStatus.DEGRADED,
            detail=(
                f"{snapshot.unpaired_positions} position(s) carrying exposure whose hedge "
                f"no longer exists - real naked exposure ({exits})"
            ),
        )
    if snapshot.valuation_status is ValuationStatus.UNAVAILABLE:
        return ComponentHealth(
            name=NAME,
            status=ComponentStatus.DEGRADED,
            detail=(
                f"{snapshot.unvalued_positions} of {snapshot.open_positions} open position(s) "
                "could not be priced from a synchronised book; aggregate equity is unavailable"
            ),
        )
    if snapshot.valuation_status is ValuationStatus.DEGRADED:
        return ComponentHealth(
            name=NAME,
            status=ComponentStatus.DEGRADED,
            detail=("the book is fully priced but degraded; inspect the snapshot risk counters"),
        )
    if unmeasured:
        return ComponentHealth(
            name=NAME,
            status=ComponentStatus.DEGRADED,
            detail=(
                f"accounting is incomplete: {', '.join(unmeasured)} cannot be measured, "
                "so realised P&L is a measurement of what was measured, not a total"
            ),
        )
    equity = "unknown" if snapshot.equity_usd is None else f"{snapshot.equity_usd:.2f} USD"
    return ComponentHealth(
        name=NAME,
        status=ComponentStatus.HEALTHY,
        detail=(
            f"equity {equity} over {snapshot.open_positions} open position(s) at "
            f"{snapshot.captured_at.isoformat()} ({exits})"
        ),
    )
