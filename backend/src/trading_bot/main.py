"""FastAPI application factory.

Composition root: this is the only module that wires configuration, logging,
the database engine and the HTTP routes together. Everything else receives what
it needs and stays independently testable.

Note: creating the engine does not open a connection, so the API starts even
when PostgreSQL is down. ``/api/v1/health/ready`` then reports 503 rather than
the process failing to boot - a monitoring surface must stay reachable.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from trading_bot import __version__
from trading_bot.api.router import api_router
from trading_bot.api.routes import root
from trading_bot.core.config import Settings, get_settings
from trading_bot.core.logging import configure_logging, get_logger
from trading_bot.db.session import dispose_engine, init_engine

logger = get_logger(__name__)


def _lifespan(
    settings: Settings,
) -> Callable[[FastAPI], AbstractAsyncContextManager[None]]:
    """Build the startup/shutdown context manager bound to these settings."""

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        init_engine(settings.database)
        logger.info(
            "app.started",
            profile=settings.profile.value,
            execution_mode=settings.execution.mode.value,
            live_execution_armed=settings.is_live_execution_armed,
            spot_markets=len(settings.markets.spot_symbols),
            perpetual_markets=len(settings.markets.perpetual_symbols),
        )
        try:
            yield
        finally:
            await dispose_engine()
            logger.info("app.stopped")

    return lifespan


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the application. Tests call this with purpose-built settings."""
    resolved = settings or get_settings()
    configure_logging(resolved.logging)

    app = FastAPI(
        title=resolved.app.name,
        version=__version__,
        summary="Quantitative crypto arbitrage research and paper-trading platform",
        docs_url="/docs" if resolved.api.docs_enabled else None,
        redoc_url=None,
        openapi_url="/openapi.json" if resolved.api.docs_enabled else None,
        lifespan=_lifespan(resolved),
    )

    if resolved.api.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=resolved.api.cors_origins,
            allow_credentials=True,
            allow_methods=["GET", "POST"],
            allow_headers=["*"],
        )

    # Root index is unversioned on purpose; the data contract stays under /api/v1.
    app.include_router(root.router)
    app.include_router(api_router)
    app.state.settings = resolved
    return app


# Import target for `uvicorn trading_bot.main:app`.
app = create_app()
