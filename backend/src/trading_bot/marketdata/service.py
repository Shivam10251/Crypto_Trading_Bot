"""The market-data service: ``make market-data`` / ``uv run trading-bot-market-data``.

Wires the Binance adapter, the engine, the recorder and the terminal view, then
runs until interrupted. Shutdown is orderly: sockets closed, the last sample
and a SHUTDOWN event flushed to the database.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
import sys
from collections import deque
from datetime import UTC, datetime

from trading_bot.core.config import MarketsConfig, Settings
from trading_bot.core.logging import get_logger
from trading_bot.db.models.enums import MarketType, Severity, SystemEventType
from trading_bot.db.session import check_connection, dispose_engine, init_engine, session_scope
from trading_bot.exchange.base import ExchangeAdapter
from trading_bot.exchange.binance import BinanceExchangeAdapter
from trading_bot.exchange.errors import ExchangeError, UnknownMarketError
from trading_bot.exchange.models import MarketDataSubscription, MarketSpec
from trading_bot.marketdata.display import CLEAR_SCREEN, render
from trading_bot.marketdata.engine import MarketDataEngine
from trading_bot.marketdata.models import MarketDataEvent
from trading_bot.marketdata.recorder import MarketDataRecorder, register_markets

logger = get_logger(__name__)


async def resolve_markets(adapter: ExchangeAdapter, markets: MarketsConfig) -> list[MarketSpec]:
    """Look the configured symbols up on the venue; refuse to start on a bad one.

    An unknown or halted symbol is a configuration error. Streaming the rest
    would leave a strategy blind to a leg it expects to have.
    """
    wanted = [(MarketType.SPOT, symbol) for symbol in markets.spot_symbols] + [
        (MarketType.PERPETUAL, symbol) for symbol in markets.perpetual_symbols
    ]
    listed: dict[MarketType, dict[str, MarketSpec]] = {}
    for kind in dict.fromkeys(kind for kind, _ in wanted):
        listed[kind] = {spec.symbol: spec for spec in await adapter.get_markets(kind)}

    specs: list[MarketSpec] = []
    missing: list[str] = []
    halted: list[str] = []
    for kind, symbol in wanted:
        spec = listed[kind].get(symbol)
        label = f"{symbol} {kind.value.lower()}"
        if spec is None:
            missing.append(label)
        elif not spec.is_active:
            halted.append(label)
        else:
            specs.append(spec)
    if missing:
        raise UnknownMarketError(f"not listed on {adapter.venue}: {', '.join(missing)}")
    if halted:
        raise UnknownMarketError(f"not trading on {adapter.venue}: {', '.join(halted)}")
    return specs


async def run_service(
    settings: Settings,
    *,
    adapter: ExchangeAdapter | None = None,
    stop: asyncio.Event | None = None,
) -> None:
    config = settings.market_data
    stop = stop or _stop_on_signals()
    async with adapter or BinanceExchangeAdapter(settings.exchange) as venue:
        specs = await resolve_markets(venue, settings.markets)
        skew_ms = await _clock_skew(venue)
        subscription = MarketDataSubscription(
            refs=tuple(spec.ref for spec in specs),
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
        recent: deque[MarketDataEvent] = deque(maxlen=6)
        engine.add_listener(recent.append)

        recorder = await _open_recorder(settings, specs) if config.persist else None
        if recorder is not None:
            engine.add_listener(recorder.record_event)
            recorder.record_event(
                _service_event(SystemEventType.STARTUP, f"started for {len(specs)} markets")
            )
            persistence = f"market_data every {config.persist_interval_ms} ms"
        else:
            persistence = "off" if not config.persist else "OFF - database unavailable"

        tasks: list[asyncio.Task[None]] = []
        try:
            async with engine:
                if recorder is not None:
                    tasks.append(asyncio.create_task(recorder.run(engine.snapshots)))
                if config.display:
                    tasks.append(
                        asyncio.create_task(
                            _display_loop(
                                engine,
                                recent,
                                venue=venue.venue,
                                skew_ms=skew_ms,
                                persistence=persistence,
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
    recent: deque[MarketDataEvent],
    *,
    venue: str,
    skew_ms: int | None,
    persistence: str,
    interval_seconds: float,
) -> None:
    tty = sys.stdout.isatty()
    while True:
        frame = render(
            engine.snapshots(),
            engine.health(),
            list(recent),
            now=datetime.now(UTC),
            venue=venue,
            clock_skew_ms=skew_ms,
            persistence=persistence,
        )
        sys.stdout.write((CLEAR_SCREEN if tty else "") + frame + "\n\n")
        sys.stdout.flush()
        await asyncio.sleep(interval_seconds)


def _stop_on_signals() -> asyncio.Event:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        # Windows has no loop signal handlers; Ctrl-C still ends the run there.
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)
    return stop
