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
    monitored_spot_markets: int
    monitored_perpetual_markets: int
    components: list[ComponentHealth]
    server_time: datetime
