"""Process entry points.

Kept separate from ``main`` so importing the app never starts a server.
"""

from __future__ import annotations

import asyncio

import uvicorn

from trading_bot.core.config import get_settings
from trading_bot.core.logging import configure_logging, get_logger
from trading_bot.db.retention import purge_expired
from trading_bot.db.session import dispose_engine, init_engine, session_scope
from trading_bot.exchange.errors import ExchangeError
from trading_bot.marketdata.service import run_service


def run_api() -> None:
    """Run the HTTP API (``uv run trading-bot-api``)."""
    settings = get_settings()
    configure_logging(settings.logging)
    uvicorn.run(
        "trading_bot.main:app",
        host=settings.api.host,
        port=settings.api.port,
        reload=settings.profile.value == "development",
        log_config=None,  # structlog owns log formatting
    )


def run_market_data() -> None:
    """Stream live market data (``uv run trading-bot-market-data``)."""
    settings = get_settings()
    configure_logging(settings.logging)
    try:
        asyncio.run(run_service(settings))
    except ExchangeError as exc:
        # A mistyped symbol or an unreachable venue at startup: say so plainly.
        get_logger(__name__).error("market_data.startup_failed", error=str(exc))
        raise SystemExit(1) from exc


def run_retention_purge() -> None:
    """Delete raw market data past its retention window.

    Meant for a cron entry or a manual run; the phases that add long-running
    services can call ``purge_expired`` directly on their own schedule.
    """
    settings = get_settings()
    configure_logging(settings.logging)
    logger = get_logger(__name__)

    async def _purge() -> None:
        init_engine(settings.database)
        try:
            async with session_scope() as session:
                deleted = await purge_expired(session, settings.retention)
            logger.info("retention.completed", deleted=deleted)
        finally:
            await dispose_engine()

    asyncio.run(_purge())


if __name__ == "__main__":  # pragma: no cover
    run_api()
