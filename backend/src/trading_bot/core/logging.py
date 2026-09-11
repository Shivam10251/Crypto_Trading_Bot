"""Structured logging.

One configuration function, called once at process start. Every module obtains a
logger via ``get_logger(__name__)``; nothing else touches the logging stack.

Rationale: trading systems are audited after the fact, so logs must be
machine-parseable (JSON in paper/production) and carry stable event names.
Secrets are redacted centrally by ``_redact`` so a careless call site cannot
leak an API key.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog

from trading_bot.core.config import LoggingConfig

_REDACT_KEYS = frozenset(
    {"password", "api_key", "api_secret", "secret", "token", "authorization", "signature"}
)
_REDACTED = "***"

_configured = False


def _redact(
    _logger: Any, _method: str, event_dict: structlog.types.EventDict
) -> structlog.types.EventDict:
    """Mask obvious credential fields anywhere in the event payload."""
    for key in list(event_dict):
        if key.lower() in _REDACT_KEYS:
            event_dict[key] = _REDACTED
    return event_dict


def configure_logging(config: LoggingConfig, *, force: bool = False) -> None:
    """Install the structlog + stdlib pipeline. Idempotent unless ``force``."""
    global _configured
    if _configured and not force:
        return

    level = logging.getLevelNamesMapping()[config.level]

    shared: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
        _redact,
    ]

    renderer: structlog.types.Processor
    if config.format == "json":
        renderer = structlog.processors.JSONRenderer()
        shared.append(structlog.processors.format_exc_info)
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())
        shared.append(structlog.processors.ExceptionPrettyPrinter())

    # stdlib LoggerFactory (not PrintLogger) so `add_logger_name` works and
    # uvicorn / sqlalchemy records share one stream, level and format.
    structlog.configure(
        processors=[*shared, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=False,
    )

    logging.basicConfig(format="%(message)s", stream=sys.stderr, level=level, force=True)
    for noisy in ("uvicorn.access", "uvicorn.error"):
        logging.getLogger(noisy).setLevel(max(level, logging.INFO))
    # Transport libraries log every frame and request at DEBUG, which would bury
    # the application's own events under dozens of lines per second.
    for chatty in ("websockets", "httpcore", "httpx"):
        logging.getLogger(chatty).setLevel(max(level, logging.WARNING))

    # SQLAlchemy emits every statement at INFO, which drowns the log at DEBUG
    # level. Setting echo_sql=True raises this logger itself when SQL is wanted.
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)

    _configured = True


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Bound logger for a module. Safe to call before ``configure_logging``."""
    return structlog.get_logger(name)  # type: ignore[no-any-return]
