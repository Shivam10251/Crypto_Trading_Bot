"""``trading-bot-backtest``: run, inspect, cancel and report backtests.

    trading-bot-backtest run --start 2026-09-11T08:00:00Z --end 2026-09-11T12:00:00Z \\
        --symbol BTCUSDT [--symbol ETHUSDT] [--set strategy.spot_perp_basis.min_net_edge_bps=5]
    trading-bot-backtest status <run-uid>
    trading-bot-backtest cancel <run-uid>
    trading-bot-backtest report <run-uid> [--json]
    trading-bot-backtest list
    trading-bot-backtest capture [--minutes 60]

Exit codes: 0 completed, 3 finished INCOMPLETE, 1 failed, 130 cancelled, 2 bad usage.
``--set`` overrides are recorded in the run's configuration snapshot and hash,
so an overridden run is never mistaken for a default one.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import yaml

from trading_bot.backtest.capture_status import capture_status, render_capture
from trading_bot.backtest.loop import guard_sessions
from trading_bot.backtest.report import build_report
from trading_bot.backtest.report_render import render
from trading_bot.backtest.runs import RunIdentity, RunStore
from trading_bot.backtest.service import run_backtest
from trading_bot.core.config import Settings, get_settings
from trading_bot.core.logging import configure_logging
from trading_bot.db.models.enums import BacktestRunStatus, MarketType
from trading_bot.db.session import dispose_engine, init_engine, session_scope
from trading_bot.exchange.models import MarketRef

EXIT_CODES = {
    BacktestRunStatus.COMPLETED: 0,
    BacktestRunStatus.INCOMPLETE: 3,
    BacktestRunStatus.FAILED: 1,
    BacktestRunStatus.CANCELLED: 130,
}


def parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError(f"{value!r} needs a timezone, e.g. 2026-09-11T08:00:00Z")
    return parsed.astimezone(UTC)


def parse_market(value: str) -> MarketRef:
    try:
        venue, symbol, market_type = value.split(":")
        return MarketRef(venue, symbol.upper(), MarketType(market_type.upper()))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"{value!r} is not venue:SYMBOL:SPOT|PERPETUAL|FUTURE"
        ) from exc


def apply_overrides(settings: Settings, overrides: Sequence[str]) -> Settings:
    """``section.key=value`` pairs, values parsed as YAML, re-validated in full."""
    if not overrides:
        return settings
    data: dict[str, Any] = settings.model_dump(mode="python")
    for override in overrides:
        path, separator, raw = override.partition("=")
        if not separator or not path:
            raise ValueError(f"override {override!r} is not key=value")
        keys = path.split(".")
        node = data
        for key in keys[:-1]:
            child = node.get(key)
            if not isinstance(child, dict):
                raise ValueError(f"override {override!r}: {key!r} is not a section")
            node = child
        if keys[-1] not in node:
            raise ValueError(f"override {override!r}: unknown setting {keys[-1]!r}")
        node[keys[-1]] = yaml.safe_load(raw)
    return Settings(**data)


def _refs(settings: Settings, args: argparse.Namespace) -> tuple[MarketRef, ...]:
    refs: list[MarketRef] = list(args.market or [])
    for symbol in args.symbol or []:
        refs.append(MarketRef(settings.exchange.venue, symbol.upper(), MarketType.SPOT))
        refs.append(MarketRef(settings.exchange.venue, symbol.upper(), MarketType.PERPETUAL))
    if not refs:
        venue = settings.exchange.venue
        refs = [MarketRef(venue, s, MarketType.SPOT) for s in settings.markets.spot_symbols]
        refs += [
            MarketRef(venue, s, MarketType.PERPETUAL) for s in settings.markets.perpetual_symbols
        ]
    return tuple(dict.fromkeys(refs))


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="trading-bot-backtest")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="replay recorded market data through the pipeline")
    run.add_argument("--start", type=parse_time, required=True)
    run.add_argument("--end", type=parse_time, required=True)
    run.add_argument("--symbol", action="append", help="both legs of SYMBOL on the venue")
    run.add_argument("--market", action="append", type=parse_market, help="venue:SYMBOL:TYPE")
    run.add_argument("--set", action="append", default=[], metavar="KEY=VALUE")
    run.add_argument("--json", action="store_true", help="print the report as JSON")
    for name, text in (
        ("status", "show a run's state"),
        ("cancel", "ask a run to stop"),
        ("report", "print what a run measured"),
    ):
        sub = commands.add_parser(name, help=text)
        sub.add_argument("run_uid", type=uuid.UUID)
        if name == "report":
            sub.add_argument("--json", action="store_true")
    commands.add_parser("list", help="the most recent runs")
    capture = commands.add_parser("capture", help="what replay capture recorded recently")
    capture.add_argument("--minutes", type=float, default=60.0)
    args = parser.parse_args(argv)

    try:
        settings = apply_overrides(get_settings(), getattr(args, "set", []))
    except ValueError as exc:
        parser.error(str(exc))
    configure_logging(settings.logging)
    if args.command == "run":
        if args.end <= args.start:
            parser.error("--end must be after --start")
        outcome = run_backtest(
            settings,
            start=args.start,
            end=args.end,
            refs=_refs(settings, args),
            on_created=_announce,
        )
        report = asyncio.run(_report(settings, outcome.run.run_uid, as_json=args.json))
        print(json.dumps(report, indent=2) if args.json else report)
        raise SystemExit(EXIT_CODES[outcome.status])
    raise SystemExit(asyncio.run(_inspect(settings, args)))


def _announce(run: RunIdentity, _engine: object) -> None:
    """The uid, the moment the row exists - so a long run can be watched or cancelled.

    On stderr, flushed: stdout carries only the report, so ``--json`` output
    stays parseable.
    """
    print(
        f"backtest {run.run_uid} created; trading-bot-backtest status|cancel {run.run_uid}",
        file=sys.stderr,
        flush=True,
    )


async def _report(settings: Settings, run_uid: uuid.UUID, *, as_json: bool = True) -> Any:
    init_engine(settings.database)
    try:
        report = await build_report(session_scope, run_uid)
        return report.as_dict() if as_json else render(report)
    finally:
        await dispose_engine()


async def _inspect(settings: Settings, args: argparse.Namespace) -> int:
    init_engine(settings.database)
    try:
        runs = RunStore(guard_sessions(session_scope))
        await runs.recover_orphans(
            stale_after=timedelta(seconds=settings.backtest.orphan_after_seconds)
        )
        if args.command == "capture":
            status = await capture_status(
                session_scope,
                venue=settings.exchange.venue,
                until=datetime.now(UTC),
                window=timedelta(minutes=args.minutes),
            )
            print(render_capture(status))
            return 0
        if args.command == "list":
            for row in await runs.recent():
                print(
                    f"{row.run_uid}  {row.status.value:<10}  {row.requested_start.isoformat()} -> "
                    f"{row.requested_end.isoformat()}  {', '.join(row.markets)}"
                )
            return 0
        if args.command == "report":
            try:
                report = await build_report(session_scope, args.run_uid)
            except LookupError as exc:
                print(exc, file=sys.stderr)
                return 1
            print(json.dumps(report.as_dict(), indent=2) if args.json else render(report))
            return 0
        if args.command == "cancel":
            cancelled = await runs.request_cancel(args.run_uid)
            if cancelled is None:
                print(f"no backtest run {args.run_uid}", file=sys.stderr)
                return 1
            if cancelled.status is BacktestRunStatus.RUNNING:
                print(f"{cancelled.run_uid} cancel requested; it stops at its next tick")
                return 0
            print(f"{cancelled.run_uid} is {cancelled.status.value}")
            return 0 if cancelled.status is BacktestRunStatus.CANCELLED else 1
        found = await runs.get(args.run_uid)
        if found is None:
            print(f"no backtest run {args.run_uid}", file=sys.stderr)
            return 1
        print(
            f"{found.run_uid}  {found.status.value}\n"
            f"  requested {found.requested_start.isoformat()} -> "
            f"{found.requested_end.isoformat()}\n"
            f"  started {found.started_at}  heartbeat {found.heartbeat_at}  "
            f"completed {found.completed_at}\n"
            f"  events: init {found.initialization_events}  accepted {found.events_accepted}  "
            f"rejected {found.events_rejected}  replayed {found.events_replayed}\n"
            f"  evaluations {found.evaluations}  "
            f"orders {found.orders_recorded}  fills {found.fills_recorded}  "
            f"trades {found.trades_completed}\n"
            f"  cancel requested {found.cancel_requested_at}  failure {found.failure_reason}"
        )
        return 0
    finally:
        await dispose_engine()


if __name__ == "__main__":  # pragma: no cover
    main()
