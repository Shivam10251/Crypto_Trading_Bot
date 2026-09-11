"""Process entry points.

Kept separate from ``main`` so importing the app never starts a server.
"""

from __future__ import annotations

import uvicorn

from trading_bot.core.config import get_settings
from trading_bot.core.logging import configure_logging


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


if __name__ == "__main__":  # pragma: no cover
    run_api()
