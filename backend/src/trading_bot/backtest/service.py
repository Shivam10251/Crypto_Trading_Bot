"""One backtest, end to end: the function the CLI and the tests both call.

It owns the event loop, because virtual time needs ``ReplayEventLoop``: a
backtest cannot be started from inside another running loop, and the helpers
here say so rather than silently falling back to wall-clock sleeps.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta

from trading_bot.backtest.clock import ReplayClock
from trading_bot.backtest.engine import BacktestEngine, BacktestOutcome
from trading_bot.backtest.loop import guard_sessions, run_replay
from trading_bot.backtest.postgres_source import PostgresHistoricalSource
from trading_bot.backtest.runs import RunIdentity, RunStore, code_revision, config_snapshot
from trading_bot.backtest.source import DatasetRequest
from trading_bot.core.config import Settings
from trading_bot.db.session import dispose_engine, get_engine, init_engine, session_scope
from trading_bot.exchange.models import MarketRef

#: Called once the run row exists, before replay starts - how a caller learns
#: the run id early (the CLI prints it, a test can cancel it). May be async.
RunCreated = Callable[[RunIdentity, BacktestEngine], Awaitable[None] | None]


def run_backtest(
    settings: Settings,
    *,
    start: datetime,
    end: datetime,
    refs: tuple[MarketRef, ...],
    on_created: RunCreated | None = None,
) -> BacktestOutcome:
    clock = ReplayClock(start)
    return run_replay(
        execute_backtest(
            settings, start=start, end=end, refs=refs, clock=clock, on_created=on_created
        ),
        clock,
    )


async def execute_backtest(
    settings: Settings,
    *,
    start: datetime,
    end: datetime,
    refs: tuple[MarketRef, ...],
    clock: ReplayClock,
    on_created: RunCreated | None = None,
) -> BacktestOutcome:
    init_engine(settings.database)
    try:
        sessions = guard_sessions(session_scope)
        runs = RunStore(sessions)
        await runs.recover_orphans(
            stale_after=timedelta(seconds=settings.backtest.orphan_after_seconds)
        )
        request = DatasetRequest(start, end, refs, batch_size=settings.backtest.batch_size)
        source = PostgresHistoricalSource(get_engine())
        # The row first: its uid is announced before anything slow happens,
        # so a run can be inspected or cancelled while it reads its dataset.
        run = await runs.create(
            source=source.name,
            start=start,
            end=end,
            refs=refs,
            snapshot=config_snapshot(settings),
            revision=code_revision(),
        )
        engine = BacktestEngine(
            settings,
            source=source,
            session_factory=sessions,
            runs=runs,
            request=request,
            clock=clock,
        )
        if on_created is not None:
            announced = on_created(run, engine)
            if inspect.isawaitable(announced):
                await announced
        return await engine.run(run)
    finally:
        await dispose_engine()
