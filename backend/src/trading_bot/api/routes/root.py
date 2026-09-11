"""Service index at ``/``.

The data contract lives under ``/api/v1``; opening the bare host would
otherwise return a bare 404, which reads as a broken server. This route answers
with what the service is and where to go next.

Deliberately excluded from the OpenAPI schema: it is a discovery aid, not part
of the versioned contract.
"""

from __future__ import annotations

from fastapi import APIRouter

from trading_bot import __version__
from trading_bot.api.deps import SettingsDep
from trading_bot.api.router import API_PREFIX
from trading_bot.api.schemas import ServiceIndexResponse

router = APIRouter()


@router.get("/", response_model=ServiceIndexResponse, include_in_schema=False)
async def service_index(settings: SettingsDep) -> ServiceIndexResponse:
    """Human-friendly landing payload for the API host."""
    return ServiceIndexResponse(
        service=settings.app.name,
        version=__version__,
        profile=settings.profile.value,
        status="ok",
        docs="/docs" if settings.api.docs_enabled else None,
        dashboard="http://localhost:5173",
        endpoints={
            "health": f"{API_PREFIX}/health",
            "readiness": f"{API_PREFIX}/health/ready",
            "system_status": f"{API_PREFIX}/system-status",
        },
    )
