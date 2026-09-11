"""API router aggregation.

All routes are mounted under ``/api/v1`` so the contract can evolve without
breaking a deployed dashboard.
"""

from __future__ import annotations

from fastapi import APIRouter

from trading_bot.api.routes import health

API_PREFIX = "/api/v1"

api_router = APIRouter(prefix=API_PREFIX)
api_router.include_router(health.router)
