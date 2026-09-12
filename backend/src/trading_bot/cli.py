"""Process entry points.

Kept separate from ``main`` so importing the app never starts a server.
"""

from __future__ import annotations

import argparse
import asyncio

import uvicorn

from trading_bot.core.config import get_settings
from trading_bot.core.logging import configure_logging, get_logger
from trading_bot.db.retention import purge_expired
from trading_bot.db.session import dispose_engine, init_engine, session_scope
from trading_bot.exchange.errors import ExchangeError
from trading_bot.marketdata.service import run_service
from trading_bot.opportunities.recost import recost
from trading_bot.risk.kill_switch import KillSwitchState
from trading_bot.risk.store import RiskEventStore
from trading_bot.strategy.fees import FeeSchedule, OrderRole


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


def run_recost() -> None:
    """Re-price stored opportunities under the current fee schedule.

    Reports; never rewrites. A stored row is what the strategy believed at the
    time, and overwriting it would destroy the only record of that.
    """
    settings = get_settings()
    configure_logging(settings.logging)
    logger = get_logger(__name__)

    async def _recost() -> None:
        init_engine(settings.database)
        try:
            async with session_scope() as session:
                report = await recost(
                    session,
                    FeeSchedule.from_config(settings.costs),
                    entry_role=OrderRole(settings.costs.entry_role.upper()),
                    exit_role=OrderRole(settings.costs.exit_role.upper()),
                )
        finally:
            await dispose_engine()
        print(report.describe())
        logger.info("recost.completed", opportunities=report.total)

    asyncio.run(_recost())


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


def run_risk_control() -> None:
    """Inspect or flip the durable kill switch (``uv run trading-bot-risk``).

    Deliberately a CLI, not an HTTP endpoint: nothing in this codebase
    authenticates a caller yet, and an unauthenticated public re-arm endpoint
    is explicitly out of bounds. The dashboard (Phase 12) can wrap this once
    an authenticated API surface exists; until then, re-arming requires shell
    access to the machine the bot runs on, and every action is still written
    to ``risk_events`` with who triggered it and why.
    """
    parser = argparse.ArgumentParser(prog="trading-bot-risk")
    subparsers = parser.add_subparsers(dest="action", required=True)
    subparsers.add_parser("status", help="show whether trading is currently halted")
    for name in ("kill", "rearm"):
        sub = subparsers.add_parser(name, help=f"{name} the kill switch")
        sub.add_argument("--who", required=True, help="operator or system identity")
        sub.add_argument("--reason", required=True, help="why, for the audit trail")
    args = parser.parse_args()

    settings = get_settings()
    configure_logging(settings.logging)
    logger = get_logger(__name__)

    async def _run() -> None:
        init_engine(settings.database)
        try:
            store = RiskEventStore(session_scope)
            switch = KillSwitchState(store, session_scope)
            await switch.load()
            if args.action == "status":
                state = "HALTED" if switch.is_active else "clear"
                print(f"{state}: {switch.blocked_reason() or 'trading is not halted'}")
                return
            if args.action == "kill":
                verdict = await switch.trigger(who=args.who, reason=args.reason)
            else:
                verdict = await switch.rearm(who=args.who, reason=args.reason)
            if verdict.risk_event_id is None:
                # Either direction is a failure worth a nonzero exit. A kill
                # that was not written stops only *this* process's view of
                # the world: a running service polling durable state will
                # never see it, so the operator must know the command did not
                # do what they asked.
                logger.error("risk_control.not_durable", action=args.action, who=args.who)
                print(f"{args.action} was NOT durably recorded; check logs before relying on it")
                if args.action == "rearm":
                    print("the switch remains engaged")
                else:
                    print(
                        "running services will NOT observe this kill; retry once the database is up"
                    )
                raise SystemExit(1)
            print(f"{args.action} recorded as risk_events.id={verdict.risk_event_id}")
        finally:
            await dispose_engine()

    asyncio.run(_run())


if __name__ == "__main__":  # pragma: no cover
    run_api()
