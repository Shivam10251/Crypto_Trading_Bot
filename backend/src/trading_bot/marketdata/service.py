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
from datetime import UTC, datetime
from decimal import Decimal

from trading_bot.core.config import Settings
from trading_bot.core.logging import get_logger
from trading_bot.db.models.enums import Severity, SystemEventType
from trading_bot.db.session import check_connection, dispose_engine, init_engine, session_scope
from trading_bot.exchange.base import ExchangeAdapter
from trading_bot.exchange.binance import BinanceExchangeAdapter
from trading_bot.exchange.errors import ExchangeError
from trading_bot.exchange.models import MarketDataSubscription, MarketSpec
from trading_bot.marketdata.engine import MarketDataEngine
from trading_bot.marketdata.models import MarketDataEvent
from trading_bot.marketdata.recorder import MarketDataRecorder, register_markets
from trading_bot.monitoring.display import CLEAR_SCREEN, FRAME_OVERHEAD, FrameContext, render
from trading_bot.monitoring.monitor import MarketMonitor
from trading_bot.monitoring.universe import select_universe

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

        recorder = await _open_recorder(settings, list(universe.specs)) if config.persist else None
        if recorder is not None:
            engine.add_listener(recorder.record_event)
            recorder.record_event(
                _service_event(SystemEventType.STARTUP, f"started: {universe.describe()}")
            )
            persistence = f"market_data every {config.persist_interval_ms} ms"
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
                if config.display:
                    tasks.append(
                        asyncio.create_task(
                            _display_loop(
                                engine,
                                monitor,
                                recent,
                                context,
                                interval_seconds=config.display_interval_ms / 1000,
                            )
                        )
                    )
                await stop.wait()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if recorder is not None:
                recorder.record_event(_service_event(SystemEventType.SHUTDOWN, "stopped"))
                await recorder.flush(engine.snapshots())
                await dispose_engine()
    logger.info("market_data.service_stopped")


def _service_event(event_type: SystemEventType, message: str) -> MarketDataEvent:
    return MarketDataEvent(
        event_type=event_type,
        severity=Severity.INFO,
        message=f"market-data service {message}",
        occurred_at=datetime.now(UTC),
    )


async def _open_recorder(settings: Settings, specs: list[MarketSpec]) -> MarketDataRecorder | None:
    """Persistence is best effort: without a database the live view still runs."""
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
    return MarketDataRecorder(
        market_ids,
        session_scope,
        interval_seconds=settings.market_data.persist_interval_ms / 1000,
    )


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
) -> None:
    tty = sys.stdout.isatty()
    interval = interval_seconds if tty else max(interval_seconds, _UNATTENDED_INTERVAL_SECONDS)
    while True:
        rows = max(5, shutil.get_terminal_size().lines - FRAME_OVERHEAD) if tty else None
        frame = render(
            monitor.metrics(),
            monitor.summary(),
            engine.health(),
            list(recent),
            now=datetime.now(UTC),
            context=context,
            max_rows=rows,
        )
        sys.stdout.write((CLEAR_SCREEN if tty else "") + frame + "\n\n")
        sys.stdout.flush()
        await asyncio.sleep(interval)


def _stop_on_signals() -> asyncio.Event:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        # Windows has no loop signal handlers; Ctrl-C still ends the run there.
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)
    return stop
