"""The market-data service: ``make market-data`` / ``uv run trading-bot-market-data``.

Chooses the markets, streams them through the engine, samples them through the
monitor, persists quotes and events, and draws the terminal view - until
interrupted. Shutdown is orderly: sockets closed, the last sample and a
SHUTDOWN event flushed to the database.
"""

from __future__ import annotations

import asyncio
import contextlib
import shutil
import signal
import sys
from collections import deque
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal

from trading_bot.core.config import Settings
from trading_bot.core.logging import get_logger
from trading_bot.db.models.enums import Severity, SystemEventType
from trading_bot.db.session import check_connection, dispose_engine, init_engine, session_scope
from trading_bot.exchange.base import ExchangeAdapter
from trading_bot.exchange.binance import BinanceExchangeAdapter
from trading_bot.exchange.errors import ExchangeError
from trading_bot.exchange.models import MarketDataSubscription, MarketRef, MarketSpec
from trading_bot.marketdata.engine import MarketDataEngine
from trading_bot.marketdata.funding import FundingTracker
from trading_bot.marketdata.models import MarketDataEvent
from trading_bot.marketdata.recorder import MarketDataRecorder, register_markets
from trading_bot.monitoring.display import CLEAR_SCREEN, FRAME_OVERHEAD, FrameContext, render
from trading_bot.monitoring.monitor import MarketMonitor
from trading_bot.monitoring.strategy_view import RecordingStatus, render_evaluation
from trading_bot.monitoring.universe import select_universe
from trading_bot.opportunities.episodes import EpisodeTracker
from trading_bot.opportunities.recorder import OpportunityRecorder
from trading_bot.strategy.base import StrategyContext
from trading_bot.strategy.costs import TransactionCostModel
from trading_bot.strategy.registry import build_strategies
from trading_bot.strategy.runner import StrategyRunner

logger = get_logger(__name__)

# Piped output gets a full frame this often rather than every refresh.
_UNATTENDED_INTERVAL_SECONDS = 10.0


async def run_service(
    settings: Settings,
    *,
    adapter: ExchangeAdapter | None = None,
    stop: asyncio.Event | None = None,
) -> None:
    config = settings.market_data
    stop = stop or _stop_on_signals()
    async with adapter or BinanceExchangeAdapter(settings.exchange) as venue:
        universe = await select_universe(venue, settings.markets)
        skew_ms = await _clock_skew(venue)
        subscription = MarketDataSubscription(
            refs=universe.refs,
            include_depth=config.include_depth,
            depth_levels=config.depth_levels,
            include_ticker=config.include_ticker,
        )
        engine = MarketDataEngine(
            venue.stream_source(),
            subscription,
            config,
            snapshot_fetcher=venue.get_order_book,
            max_backoff_seconds=settings.exchange.max_reconnect_backoff_seconds,
        )
        monitor = MarketMonitor(engine, settings.monitoring, ranks=universe.ranks)
        recent: deque[MarketDataEvent] = deque(maxlen=6)
        engine.add_listener(recent.append)

        strategies = _build_strategy_layer(settings, universe.specs)
        funding = (
            FundingTracker(
                venue,
                universe.refs,
                interval_seconds=settings.strategy.spot_perp_basis.funding_refresh_seconds,
            )
            if strategies is not None
            else None
        )
        if funding is not None:
            await _prime_funding(funding)

        wants_database = config.persist or (
            settings.opportunities.persist and strategies is not None
        )
        opened = await _open_recorders(settings, list(universe.specs)) if wants_database else None
        recorder, market_ids = opened if opened is not None else (None, {})
        opportunities = (
            OpportunityRecorder(
                market_ids,
                session_scope,
                interval_seconds=settings.opportunities.flush_interval_ms / 1000,
                min_duration_ms=settings.opportunities.min_duration_ms,
            )
            if market_ids and settings.opportunities.persist and strategies is not None
            else None
        )
        episodes = EpisodeTracker() if opportunities is not None else None
        if recorder is not None:
            engine.add_listener(recorder.record_event)
            recorder.record_event(
                _service_event(SystemEventType.STARTUP, f"started: {universe.describe()}")
            )
            persistence = f"market_data every {config.persist_interval_ms} ms"
            if opportunities is not None:
                persistence += (
                    f" + opportunities every {settings.opportunities.flush_interval_ms} ms"
                )
        else:
            persistence = "off" if not config.persist else "OFF - database unavailable"
        context = FrameContext(
            venue=venue.venue,
            universe=universe.describe(),
            clock_skew_ms=skew_ms,
            persistence=persistence,
            band_bps=Decimal(str(config.liquidity_band_bps)),
            reference_notional=Decimal(str(config.reference_order_notional)),
        )

        tasks: list[asyncio.Task[None]] = []
        try:
            async with engine:
                tasks.append(asyncio.create_task(monitor.run(), name="market-monitor"))
                if recorder is not None:
                    tasks.append(asyncio.create_task(recorder.run(engine.snapshots)))
                if strategies is not None and funding is not None:
                    runner, cost_summary = strategies
                    tasks.append(asyncio.create_task(funding.run(), name="funding-tracker"))
                    if opportunities is not None:
                        tasks.append(
                            asyncio.create_task(opportunities.run(), name="opportunity-recorder")
                        )
                    tasks.append(
                        asyncio.create_task(
                            _strategy_loop(
                                runner,
                                monitor,
                                engine,
                                funding,
                                episodes,
                                opportunities,
                                interval_seconds=(settings.strategy.evaluate_interval_ms / 1000),
                            ),
                            name="strategy-runner",
                        )
                    )
                else:
                    runner, cost_summary = None, ""
                if config.display:
                    tasks.append(
                        asyncio.create_task(
                            _display_loop(
                                engine,
                                monitor,
                                recent,
                                context,
                                interval_seconds=config.display_interval_ms / 1000,
                                runner=runner if settings.strategy.display else None,
                                cost_summary=cost_summary,
                                funding=funding,
                                episodes=episodes,
                                opportunities=opportunities,
                            )
                        )
                    )
                await stop.wait()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if opportunities is not None and episodes is not None:
                # Episodes still running when the process stops are real
                # observations; closing them is what keeps them in the record.
                opportunities.record(episodes.close_all())
                await opportunities.flush()
                logger.info(
                    "opportunities.recorded",
                    opportunities=opportunities.opportunities_written,
                    signals=opportunities.signals_written,
                    unpriced=opportunities.unpriced,
                )
            if recorder is not None:
                recorder.record_event(_service_event(SystemEventType.SHUTDOWN, "stopped"))
                await recorder.flush(engine.snapshots())
            if recorder is not None or opportunities is not None:
                await dispose_engine()
    logger.info("market_data.service_stopped")


def _service_event(event_type: SystemEventType, message: str) -> MarketDataEvent:
    return MarketDataEvent(
        event_type=event_type,
        severity=Severity.INFO,
        message=f"market-data service {message}",
        occurred_at=datetime.now(UTC),
    )


async def _open_recorders(
    settings: Settings, specs: list[MarketSpec]
) -> tuple[MarketDataRecorder | None, dict[MarketRef, int]] | None:
    """Open the database and register the markets, or report it unavailable.

    Persistence is best effort: without a database the live view and the
    strategy still run, they simply keep no record. The market ids come back
    because the opportunity recorder needs them to link an opportunity's legs.
    """
    init_engine(settings.database)
    if not await check_connection():
        logger.warning(
            "market_data.persistence_disabled",
            reason="database unreachable",
            dsn=settings.database.safe_dsn(),
        )
        await dispose_engine()
        return None
    try:
        async with session_scope() as session:
            market_ids = await register_markets(session, specs)
    except Exception as exc:  # most often: migrations not applied yet
        logger.warning(
            "market_data.persistence_disabled", reason=str(exc), hint="run `make migrate`"
        )
        await dispose_engine()
        return None
    quotes = (
        MarketDataRecorder(
            market_ids,
            session_scope,
            interval_seconds=settings.market_data.persist_interval_ms / 1000,
        )
        if settings.market_data.persist
        else None
    )
    return quotes, market_ids


def _build_strategy_layer(
    settings: Settings, specs: Sequence[MarketSpec]
) -> tuple[StrategyRunner, str] | None:
    """The strategy runner and the one-line summary of its cost assumptions.

    ``None`` when evaluation is switched off or no strategy is enabled - the
    market view still runs, it simply has nothing to say about opportunities.
    """
    if not settings.strategy.evaluate or not settings.strategy.enabled:
        return None
    cost_model = TransactionCostModel(settings.costs)
    by_ref = {spec.ref: spec for spec in specs}
    runner = StrategyRunner(
        build_strategies(settings.strategy),
        StrategyContext(cost_model=cost_model, specs=by_ref),
        specs=by_ref,
    )
    return runner, cost_model.describe()


async def _prime_funding(tracker: FundingTracker) -> None:
    """One funding poll before the first evaluation.

    Without it the first cycles price nothing at all, which would read as "no
    opportunities" when it actually means "no funding data yet".
    """
    try:
        await tracker.refresh()
    except ExchangeError as exc:
        logger.warning("funding.initial_refresh_failed", error=str(exc))


async def _strategy_loop(
    runner: StrategyRunner,
    monitor: MarketMonitor,
    engine: MarketDataEngine,
    funding: FundingTracker,
    episodes: EpisodeTracker | None,
    opportunities: OpportunityRecorder | None,
    *,
    interval_seconds: float,
) -> None:
    """Evaluate every strategy against the latest snapshots, on a fixed cadence.

    Episodes are tracked here rather than in the recorder so that evaluation
    and persistence stay independent: without a database the strategy still
    runs and still draws, it simply keeps no record.
    """
    while True:
        runner.set_funding(funding.rates)
        evaluations = runner.evaluate(engine.snapshots(), monitor.metrics())
        if episodes is not None and opportunities is not None:
            opportunities.record(episodes.update(evaluations, datetime.now(UTC)))
        await asyncio.sleep(interval_seconds)


async def _clock_skew(adapter: ExchangeAdapter) -> int | None:
    try:
        return (await adapter.get_server_time()).skew_ms
    except ExchangeError as exc:
        logger.warning("market_data.clock_skew_unknown", error=str(exc))
        return None


async def _display_loop(
    engine: MarketDataEngine,
    monitor: MarketMonitor,
    recent: deque[MarketDataEvent],
    context: FrameContext,
    *,
    interval_seconds: float,
    runner: StrategyRunner | None = None,
    cost_summary: str = "",
    funding: FundingTracker | None = None,
    episodes: EpisodeTracker | None = None,
    opportunities: OpportunityRecorder | None = None,
) -> None:
    tty = sys.stdout.isatty()
    interval = interval_seconds if tty else max(interval_seconds, _UNATTENDED_INTERVAL_SECONDS)
    while True:
        rows = max(5, shutil.get_terminal_size().lines - FRAME_OVERHEAD) if tty else None
        panels = _strategy_panels(
            runner, cost_summary, funding, episodes, opportunities, max_rows=rows
        )
        if panels and rows is not None:
            # The strategy panel and the market table share one screen; give
            # the markets what is left rather than scrolling either away.
            rows = max(3, rows - sum(panel.count("\n") + 2 for panel in panels))
        frame = render(
            monitor.metrics(),
            monitor.summary(),
            engine.health(),
            list(recent),
            now=datetime.now(UTC),
            context=context,
            max_rows=rows,
        )
        body = "\n\n".join([frame, *panels]) if panels else frame
        sys.stdout.write((CLEAR_SCREEN if tty else "") + body + "\n\n")
        sys.stdout.flush()
        await asyncio.sleep(interval)


def _strategy_panels(
    runner: StrategyRunner | None,
    cost_summary: str,
    funding: FundingTracker | None,
    episodes: EpisodeTracker | None,
    opportunities: OpportunityRecorder | None,
    *,
    max_rows: int | None,
) -> list[str]:
    """One panel per strategy; empty until the first evaluation has run."""
    if runner is None:
        return []
    unknown = funding.markets_without_interval if funding is not None else set()
    recording = (
        RecordingStatus(
            episodes_open=len(episodes.open_episodes()),
            opportunities_written=opportunities.opportunities_written,
            signals_written=opportunities.signals_written,
            unpriced=opportunities.unpriced,
        )
        if episodes is not None and opportunities is not None
        else None
    )
    # A panel gets at most a third of the screen, so the market table survives.
    panel_rows = None if max_rows is None else max(3, max_rows // 3)
    return [
        render_evaluation(
            evaluation,
            cost_summary=cost_summary,
            max_rows=panel_rows,
            funding_unknown=sorted(unknown),
            recording=recording,
        )
        for evaluation in runner.evaluations()
    ]


def _stop_on_signals() -> asyncio.Event:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        # Windows has no loop signal handlers; Ctrl-C still ends the run there.
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)
    return stop
