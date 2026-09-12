"""Health, readiness and system-status endpoints.

Every subsystem here is judged by evidence it actually wrote - quotes,
opportunities, orders, risk decisions - because each runs in the market-data
process and the API cannot ask any of them anything. Subsystems no phase has
built yet are reported OFFLINE with the phase that will implement them; the
dashboard must never show an invented value.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Response, status

from trading_bot import __version__
from trading_bot.api.deps import SettingsDep
from trading_bot.api.execution_status import execution_status
from trading_bot.api.market_status import market_data_status
from trading_bot.api.risk_status import risk_status
from trading_bot.api.schemas import (
    ComponentHealth,
    ComponentStatus,
    HealthResponse,
    ReadinessResponse,
    SystemStatusResponse,
)
from trading_bot.api.strategy_status import strategy_status
from trading_bot.db.session import check_connection

router = APIRouter(tags=["system"])

# Subsystems not yet built. Kept here so exactly one place needs editing as
# each phase lands, and so nothing reports a status it cannot substantiate.
# Empty since Phase 9: Portfolio & P&L (Phase 10) has no component row of its
# own yet, and will add one when it does.
_PENDING_COMPONENTS: tuple[tuple[str, str], ...] = ()


def _now() -> datetime:
    return datetime.now(UTC)


async def _database_health() -> ComponentHealth:
    connected = await check_connection()
    return ComponentHealth(
        name="Database",
        status=ComponentStatus.HEALTHY if connected else ComponentStatus.OFFLINE,
        detail="connected" if connected else "no connection to PostgreSQL",
    )


def _pending_components() -> list[ComponentHealth]:
    return [
        ComponentHealth(name=name, status=ComponentStatus.OFFLINE, detail=detail)
        for name, detail in _PENDING_COMPONENTS
    ]


@router.get("/health", response_model=HealthResponse, summary="Liveness probe")
async def health(settings: SettingsDep) -> HealthResponse:
    """Confirms the process is running. Does not touch the database."""
    return HealthResponse(
        status="ok",
        version=__version__,
        profile=settings.profile.value,
        server_time=_now(),
    )


@router.get("/health/ready", response_model=ReadinessResponse, summary="Readiness probe")
async def readiness(response: Response) -> ReadinessResponse:
    """Reports whether dependencies are usable.

    Returns HTTP 503 when the database is unreachable so orchestrators and the
    dashboard can distinguish "up" from "usable".
    """
    components = [await _database_health()]
    ready = all(c.status is ComponentStatus.HEALTHY for c in components)
    if not ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return ReadinessResponse(ready=ready, components=components, server_time=_now())


@router.get(
    "/system-status",
    response_model=SystemStatusResponse,
    summary="Aggregate status for the dashboard",
)
async def system_status(settings: SettingsDep) -> SystemStatusResponse:
    api_component = ComponentHealth(
        name="API", status=ComponentStatus.HEALTHY, detail="serving requests"
    )
    now = _now()
    market = await market_data_status(settings, now)
    components = [
        api_component,
        await _database_health(),
        market.exchange,
        market.market_data,
        # Judged by the opportunities it recorded, the same way the feed is
        # judged by its quotes - the strategy runs in another process.
        await strategy_status(settings, now),
        # Judged by the decisions it wrote, and by the durable kill switch -
        # which is reported even when execution is off, because a halted
        # switch is what a restart would restore.
        await risk_status(settings, now),
        # Judged by the orders it wrote. "No orders" is the normal state of
        # this strategy, so it reads OFFLINE with the reason rather than as a
        # fault - and never as HEALTHY on no evidence.
        await execution_status(settings, now),
        *_pending_components(),
    ]
    return SystemStatusResponse(
        profile=settings.profile.value,
        version=__version__,
        execution_mode=settings.execution.mode.value,
        live_execution_armed=settings.is_live_execution_armed,
        exchange=settings.exchange.venue,
        monitored_spot_markets=market.monitored_spot,
        monitored_perpetual_markets=market.monitored_perpetual,
        components=components,
        server_time=_now(),
    )
