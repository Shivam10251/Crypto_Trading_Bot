"""Response models for the HTTP API.

Every field the dashboard renders is defined here, so the frontend has a typed
contract and the backend cannot silently change shape.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class ComponentStatus(StrEnum):
    """Health of a single subsystem, as shown in the dashboard status panel."""

    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    OFFLINE = "OFFLINE"


class HealthResponse(BaseModel):
    """Liveness: the process is up and serving requests."""

    status: str = Field(examples=["ok"])
    version: str
    profile: str
    server_time: datetime


class ComponentHealth(BaseModel):
    """One row of the system-health panel."""

    name: str
    status: ComponentStatus
    # Why a component is not HEALTHY, or which phase will implement it.
    detail: str


class ReadinessResponse(BaseModel):
    """Readiness: can this process serve dependent traffic right now?"""

    ready: bool
    components: list[ComponentHealth]
    server_time: datetime


class SystemStatusResponse(BaseModel):
    """Aggregate status for the dashboard.

    Components that are not implemented yet report OFFLINE with the phase that
    will build them. No value here is ever fabricated.
    """

    profile: str
    version: str
    execution_mode: str
    live_execution_armed: bool
    exchange: str
    # The market-data service's latest selection; None when the database
    # cannot say, rather than a number guessed from configuration.
    monitored_spot_markets: int | None
    monitored_perpetual_markets: int | None
    components: list[ComponentHealth]
    server_time: datetime


class ServiceIndexResponse(BaseModel):
    """What the root path returns: what this service is and where to go next."""

    service: str
    version: str
    profile: str
    status: str
    # None when API docs are disabled (production).
    docs: str | None
    dashboard: str
    endpoints: dict[str, str]
