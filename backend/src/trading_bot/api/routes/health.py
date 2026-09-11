"""Health, readiness and system-status endpoints.

These are the only endpoints Phase 0 exposes. Subsystems that later phases build
(market data, strategy, risk, execution) are reported OFFLINE with the phase
that will implement them - the dashboard must never show invented values.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Response, status

from trading_bot import __version__
from trading_bot.api.deps import SettingsDep
from trading_bot.api.market_status import market_data_status
from trading_bot.api.schemas import (
    ComponentHealth,
    ComponentStatus,
    HealthResponse,
    ReadinessResponse,
    SystemStatusResponse,
)
from trading_bot.db.session import check_connection

router = APIRouter(tags=["system"])

# Subsystems not yet built. Kept here so exactly one place needs editing as
# each phase lands, and so nothing reports a status it cannot substantiate.
_PENDING_COMPONENTS: tuple[tuple[str, str], ...] = (
    # Built in Phase 5 and evaluating inside the market-data service, but it
    # stores nothing until Phase 7 - so the API has no output to judge it by
    # and says so, rather than reporting a health it cannot substantiate.
    (
        "Strategy Engine",
        "detection runs in the market-data service; not observable here until Phase 7",
    ),
    ("Risk Engine", "not implemented until Phase 9"),
    ("Paper Execution", "not implemented until Phase 8"),
)


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
    market = await market_data_status(settings, _now())
    components = [
        api_component,
        await _database_health(),
        market.exchange,
        market.market_data,
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
