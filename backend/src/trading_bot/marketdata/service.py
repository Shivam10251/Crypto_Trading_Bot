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
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select

from trading_bot.core.config import Settings
from trading_bot.core.logging import get_logger
from trading_bot.db.models import Market, Position
from trading_bot.db.models.enums import (
    LIVE_POSITION_STATUSES,
    ExecutionMode,
    OrderType,
    Severity,
    SystemEventType,
)
from trading_bot.db.session import check_connection, dispose_engine, init_engine, session_scope
from trading_bot.exchange.base import ExchangeAdapter
from trading_bot.exchange.binance import BinanceExchangeAdapter
from trading_bot.exchange.errors import ExchangeError
from trading_bot.exchange.models import MarketDataSubscription, MarketRef, MarketSpec
from trading_bot.execution.account import PaperAccount, PaperPositionSeed
from trading_bot.execution.coordinator import ExecutionAttempt, ExecutionCoordinator
from trading_bot.execution.dispatcher import ExecutionDispatcher
from trading_bot.execution.paper import PaperExecutionAdapter
from trading_bot.execution.recorder import ExecutionRecorder
from trading_bot.execution.shadow import choose_probe, probe_signal
from trading_bot.marketdata.engine import MarketDataEngine
from trading_bot.marketdata.funding import FundingTracker
from trading_bot.marketdata.models import MarketDataEvent
from trading_bot.marketdata.recorder import MarketDataRecorder, register_markets
from trading_bot.monitoring.display import CLEAR_SCREEN, FRAME_OVERHEAD, FrameContext, render
from trading_bot.monitoring.execution_view import render_execution
from trading_bot.monitoring.monitor import MarketMonitor
from trading_bot.monitoring.strategy_view import RecordingStatus, render_evaluation
from trading_bot.monitoring.universe import select_universe
from trading_bot.opportunities.episodes import EpisodeTracker, episode_key
from trading_bot.opportunities.recorder import OpportunityRecorder
from trading_bot.portfolio.closer import PositionCloser
from trading_bot.portfolio.pnl_source import PortfolioPnlSource
from trading_bot.portfolio.service import PortfolioService
from trading_bot.portfolio.snapshots import SnapshotWriter
from trading_bot.portfolio.store import PortfolioStore
from trading_bot.portfolio.valuation import MarkReader
from trading_bot.risk.engine import RiskEngine
from trading_bot.risk.kill_switch import KillSwitchState
from trading_bot.risk.models import PnlSource
from trading_bot.risk.store import RiskEventStore
from trading_bot.strategy.base import StrategyContext
from trading_bot.strategy.costs import TransactionCostModel
from trading_bot.strategy.fees import FeeSchedule
from trading_bot.strategy.registry import build_strategies
from trading_bot.strategy.runner import (
    StrategyEvaluation,
    StrategyRunner,
)

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

        wants_execution = bool(
            strategies is not None and (settings.execution.enabled or settings.execution.shadow)
        )
        wants_database = (
            config.persist
            or (settings.opportunities.persist and strategies is not None)
            or wants_execution
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
        episodes = (
            EpisodeTracker()
            if strategies is not None and (opportunities is not None or wants_execution)
            else None
        )
        # Execution fails closed without its audit database.  Simulating fills
        # that cannot be persisted would leave exposure with no durable trail.
        paper_account = (
            await _restore_paper_account(settings) if market_ids and wants_execution else None
        )
        # Probes measure against their own ledger. It is deliberately *not*
        # restored from durable positions - shadow positions are hypothetical,
        # and seeding a probe account from them would carry yesterday's
        # hypotheses into today's measurements.
        shadow_account = (
            _paper_account(settings)
            if market_ids and wants_execution and settings.execution.shadow
            else None
        )
        paper_adapter = (
            _build_paper_adapter(settings, venue, engine, list(universe.specs), funding)
            if market_ids and wants_execution and funding is not None
            else None
        )
        coordinator = (
            _build_execution_layer(
                settings,
                paper_adapter,
                list(universe.specs),
                paper_account,
                shadow_account,
            )
            if paper_adapter is not None
            else None
        )
        executions = (
            ExecutionRecorder(
                market_ids,
                session_scope,
                interval_seconds=settings.opportunities.flush_interval_ms / 1000,
            )
            if market_ids and coordinator is not None
            else None
        )
        risk_events = (
            RiskEventStore(session_scope) if market_ids and coordinator is not None else None
        )
        # Built before the risk engine: it is the engine's realised-P&L
        # source, which is what makes the Phase 9 daily-loss and
        # consecutive-loss limits operate at all.
        portfolio_parts = _build_portfolio(settings, engine, market_ids)
        risk = (
            await _build_risk_engine(
                settings,
                paper_account,
                shadow_account,
                risk_events,
                portfolio_parts[1] if portfolio_parts is not None else None,
            )
            if risk_events is not None and paper_account is not None
            else None
        )
        risk_engine, kill_switch = risk if risk is not None else (None, None)
        portfolio = (
            _wire_portfolio(
                settings,
                portfolio_parts,
                paper_adapter,
                risk_engine,
                paper_account,
            )
            if portfolio_parts is not None
            else None
        )
        if settings.portfolio.enabled and portfolio is None:
            logger.error(
                "portfolio.disabled",
                reason="no database, or execution and its risk engine are unavailable",
            )
        # The risk engine is mandatory, not optional wiring: an execution
        # layer with a coordinator but no risk engine would place orders
        # ungated, which is exactly the invariant this phase exists to close.
        dispatcher = (
            ExecutionDispatcher(
                coordinator,
                executions,
                queue_size=settings.execution.queue_size,
                workers=settings.execution.workers,
                recent_attempts=settings.execution.recent_attempts,
                timeout_ms=settings.execution.timeout_ms,
                risk_engine=risk_engine,
            )
            if coordinator is not None and executions is not None and risk_engine is not None
            else None
        )
        attempts = dispatcher.attempts if dispatcher is not None else ()
        if wants_execution and dispatcher is None:
            logger.error(
                "execution.disabled",
                reason="durable database audit or the risk engine is unavailable",
            )
        if dispatcher is not None and kill_switch is not None:
            # A kill - from here, from the CLI, from another service - drops
            # work this process has accepted but not yet submitted, instead of
            # letting a worker pick it up and reject it one at a time.
            kill_switch.add_listener(dispatcher.purge)
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
            if executions is not None:
                persistence += " + paper orders"
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
        strategy_task: asyncio.Task[None] | None = None
        try:
            async with engine:
                if dispatcher is not None:
                    dispatcher.start()
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
                    if executions is not None:
                        tasks.append(
                            asyncio.create_task(executions.run(), name="execution-recorder")
                        )
                    if risk_events is not None:
                        tasks.append(
                            asyncio.create_task(
                                risk_events.run(
                                    interval_seconds=settings.opportunities.flush_interval_ms / 1000
                                ),
                                name="risk-event-recorder",
                            )
                        )
                    if kill_switch is not None:
                        # Durable kill-switch state is re-read on this cadence,
                        # which is what bounds how long a kill written by any
                        # other process takes to stop this one.
                        tasks.append(
                            asyncio.create_task(
                                kill_switch.run(
                                    interval_seconds=settings.risk.kill_switch_poll_ms / 1000
                                ),
                                name="risk-kill-switch-poll",
                            )
                        )
                    if portfolio is not None:
                        # Two loops, two cadences: an exit is evaluated
                        # against a live book, while the snapshot interval is
                        # also the return-sampling interval Sharpe and
                        # Sortino are annualised from.
                        tasks.append(
                            asyncio.create_task(
                                portfolio.run_snapshots(), name="portfolio-snapshots"
                            )
                        )
                        tasks.append(
                            asyncio.create_task(portfolio.run_exits(), name="portfolio-exits")
                        )
                    strategy_task = asyncio.create_task(
                        _strategy_loop(
                            runner,
                            monitor,
                            engine,
                            funding,
                            episodes,
                            opportunities,
                            settings,
                            dispatcher,
                            interval_seconds=(settings.strategy.evaluate_interval_ms / 1000),
                        ),
                        name="strategy-runner",
                    )
                    tasks.append(strategy_task)
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
                                attempts=attempts,
                            )
                        )
                    )
                await stop.wait()
                # Stop the producer first, then drain accepted execution while
                # the market feed is still live. Draining after __aexit__
                # would turn queued work into shutdown-induced stale failures.
                if strategy_task is not None:
                    strategy_task.cancel()
                    await asyncio.gather(strategy_task, return_exceptions=True)
                if dispatcher is not None:
                    await dispatcher.stop()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if dispatcher is not None:
                await dispatcher.stop()
            if executions is not None:
                # Orders must exist before the opportunity recorder back-links
                # their signal and position provenance below.
                await executions.flush()
                logger.info(
                    "execution.recorded",
                    orders=executions.orders_written,
                    fills=executions.fills_written,
                    unhedged=executions.unhedged,
                )
            if portfolio is not None:
                # One last snapshot after execution has drained, so the final
                # equity point describes the book the process is leaving
                # behind rather than the one it had a minute ago.
                with contextlib.suppress(Exception):
                    await portfolio.snapshot()
                logger.info(
                    "portfolio.recorded",
                    snapshots=portfolio.snapshots_written,
                    pnl_rows=portfolio.pnl_rows_written,
                    failures=portfolio.snapshot_failures,
                )
            if risk_events is not None:
                await risk_events.flush()
                logger.info(
                    "risk.events_recorded",
                    persisted=risk_events.persisted,
                    queued_written=risk_events.queued_written,
                    persist_failures=risk_events.persist_failures,
                )
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
            if (
                recorder is not None
                or opportunities is not None
                or executions is not None
                or risk_events is not None
                or portfolio is not None
            ):
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
    by_ref = {spec.ref: spec for spec in specs}
    # The specs go to the cost model as well as to the strategy: they carry
    # any fee rate the venue publishes, and a rate nothing is handed cannot
    # override the configured guess.
    cost_model = TransactionCostModel(settings.costs, specs=by_ref)
    runner = StrategyRunner(
        build_strategies(settings.strategy),
        StrategyContext(cost_model=cost_model, specs=by_ref),
        specs=by_ref,
    )
    return runner, cost_model.describe()


def _build_paper_adapter(
    settings: Settings,
    venue: ExchangeAdapter,
    engine: MarketDataEngine,
    specs: Sequence[MarketSpec],
    funding: FundingTracker,
) -> PaperExecutionAdapter | None:
    """The simulator, or ``None`` when execution is off.

    Live execution cannot arrive here: the adapter is ``PaperExecutionAdapter``
    unconditionally, and the live one does not exist until Phase 17. The
    configuration guards in ``core.config`` refuse to boot an armed live
    process, so there are two independent reasons nothing real can be sent.

    Built separately from the coordinator since Phase 10, because the exit
    path needs the same adapter: a close has to fill against the same book,
    with the same latency and the same cache of client order ids, or a retried
    close would re-simulate a fill the entry path already recorded.
    """
    config = settings.execution
    if not (config.enabled or config.shadow):
        return None
    if config.mode.value != "paper":
        logger.critical(
            "execution.live_adapter_unavailable",
            detail="live mode cannot fall back to paper execution",
        )
        return None
    by_ref = {spec.ref: spec for spec in specs}
    return PaperExecutionAdapter(
        engine,
        config,
        fees=FeeSchedule.from_config(settings.costs),
        specs=lambda ref: by_ref.get(ref),
        mark_prices=lambda ref: funding.rates[ref].mark_price if ref in funding.rates else None,
        average_prices=venue.get_average_price,
    )


def _build_execution_layer(
    settings: Settings,
    adapter: PaperExecutionAdapter,
    specs: Sequence[MarketSpec],
    account: PaperAccount | None,
    shadow_account: PaperAccount | None = None,
) -> ExecutionCoordinator:
    """The coordinator that sends both legs of an entry at once."""
    config = settings.execution
    by_ref = {spec.ref: spec for spec in specs}
    return ExecutionCoordinator(
        adapter,
        order_type=OrderType(config.entry_order_type.upper()),
        specs=lambda ref: by_ref.get(ref),
        # The strategy's gate, restated where an order would actually be sent.
        allow_spot_short=settings.strategy.spot_perp_basis.allow_spot_short,
        max_leg_skew_ms=config.max_leg_skew_ms,
        account=account,
        shadow_account=shadow_account,
    )


async def _build_risk_engine(
    settings: Settings,
    account: PaperAccount,
    shadow_account: PaperAccount | None,
    store: RiskEventStore,
    pnl_source: PnlSource | None = None,
) -> tuple[RiskEngine, KillSwitchState]:
    """The risk engine and the kill switch it reads, state already restored.

    Loading the kill switch reads its own audit trail; a database it cannot
    reach here fails closed (the switch loads as active), which then rejects
    every signal until an operator can see why - never "assume clear". The
    switch comes back too so the caller can poll it and subscribe to it.

    ``pnl_source`` is Phase 10's. Passing ``None`` leaves the daily-loss and
    consecutive-loss controls reporting unavailable exactly as they did
    before one existed - which is what the portfolio service being switched
    off honestly means, not a reason to gate against zero.
    """
    kill_switch = KillSwitchState(store, session_scope, mode=ExecutionMode.PAPER)
    await kill_switch.load()
    engine = RiskEngine(
        settings.risk,
        account,
        store,
        kill_switch,
        shadow_account=shadow_account,
        pnl_source=pnl_source,
        mode=ExecutionMode.PAPER,
    )
    return engine, kill_switch


def _wire_portfolio(
    settings: Settings,
    parts: tuple[PortfolioStore, PortfolioPnlSource, MarkReader, SnapshotWriter],
    adapter: PaperExecutionAdapter | None,
    risk_engine: RiskEngine | None,
    account: PaperAccount | None,
) -> PortfolioService | None:
    """The portfolio service, with an exit loop only when it can close safely.

    Snapshots do not need execution: valuing the book and writing P&L is
    useful on its own, and it is what supplies the risk engine's realised
    P&L. Closing does need it - an adapter to send to and a risk engine to
    audit the decision - so without either, the service still runs and simply
    never closes anything, which is stated rather than silently assumed.
    """
    store, pnl_source, marks, writer = parts
    closer = (
        PositionCloser(
            store=store,
            session_factory=session_scope,
            adapter=adapter,
            risk=risk_engine,
            marks=marks,
            account=account,
            config=settings.portfolio.exits,
            venue=settings.exchange.venue,
            mode=ExecutionMode.PAPER,
        )
        if settings.portfolio.exits.enabled and adapter is not None and risk_engine is not None
        else None
    )
    if settings.portfolio.exits.enabled and closer is None:
        logger.error(
            "portfolio.exits_disabled",
            reason="closing needs both an execution adapter and a risk engine",
        )
    return PortfolioService(
        store=store,
        writer=writer,
        marks=marks,
        closer=closer,
        pnl_source=pnl_source,
        config=settings.portfolio,
        venue=settings.exchange.venue,
    )


def _build_portfolio(
    settings: Settings,
    engine: MarketDataEngine,
    market_ids: dict[MarketRef, int],
) -> tuple[PortfolioStore, PortfolioPnlSource, MarkReader, SnapshotWriter] | None:
    """The portfolio's durable half, built before the risk engine needs it.

    Returns ``None`` when the subsystem is switched off or has no database:
    without durable rows there is no realised P&L to report, and reporting
    one derived from memory alone would not survive a restart.
    """
    if not settings.portfolio.enabled or not market_ids:
        return None
    store = PortfolioStore(session_scope, mode=ExecutionMode.PAPER)
    pnl_source = PortfolioPnlSource(
        store,
        venue=settings.exchange.venue,
        # Risk only needs to know whether the threshold was reached. Reading
        # this many newest trades preserves that answer without rescanning
        # the entire history every second.
        streak_limit=settings.risk.max_consecutive_losses,
        refresh_interval=timedelta(milliseconds=settings.portfolio.pnl_refresh_ms),
    )
    marks = MarkReader(engine, max_book_age_ms=settings.portfolio.mark_max_age_ms)
    writer = SnapshotWriter(
        store,
        session_scope,
        initial_cash_usd=Decimal(str(settings.execution.paper_cash_usd)),
        # Fees paid in BNB never touch the cash balance, so replaying cash
        # from fills must not subtract them.
        fees_paid_in_cash=not settings.costs.pay_fees_in_bnb,
        interval=timedelta(milliseconds=settings.portfolio.snapshot_interval_ms),
        min_return_observations=settings.portfolio.min_return_observations,
        risk_free_rate_annual_pct=settings.portfolio.risk_free_rate_annual_pct,
    )
    return store, pnl_source, marks, writer


def _paper_account(settings: Settings) -> PaperAccount:
    """An empty paper ledger built from configuration."""
    return PaperAccount(
        settings.execution,
        settings.risk,
        pays_fees_in_bnb=settings.costs.pay_fees_in_bnb,
        max_fee_bps=Decimal(
            str(max(settings.costs.spot_taker_fee_bps, settings.costs.perp_taker_fee_bps))
        ),
    )


async def _restore_paper_account(settings: Settings) -> PaperAccount:
    """Seed balances and exposure from all durable paper activity.

    Seeded at the **remaining** open size, not the size originally opened.
    Since Phase 10 a position can be partially closed, and restoring it whole
    would restore exposure that has already been given back - the account
    would drift further from the record on every restart. ``CLOSING`` rows are
    included for the opposite reason: their exposure still exists.
    """
    account = _paper_account(settings)
    balance_writer = SnapshotWriter(
        PortfolioStore(session_scope, mode=ExecutionMode.PAPER),
        session_scope,
        initial_cash_usd=Decimal(str(settings.execution.paper_cash_usd)),
        fees_paid_in_cash=not settings.costs.pay_fees_in_bnb,
        interval=timedelta(milliseconds=settings.portfolio.snapshot_interval_ms),
        min_return_observations=settings.portfolio.min_return_observations,
        risk_free_rate_annual_pct=settings.portfolio.risk_free_rate_annual_pct,
    )
    cash, paid_fees = await balance_writer.balances()
    bnb_balance: Decimal | None = None
    if settings.costs.pay_fees_in_bnb:
        if settings.execution.paper_bnb_price_usd is None:  # settings validation guards this
            raise RuntimeError("BNB fee payment needs a BNB/USD price")
        bnb_balance = Decimal(str(settings.execution.paper_bnb_balance)) - paid_fees / Decimal(
            str(settings.execution.paper_bnb_price_usd)
        )
        if bnb_balance < 0:
            raise RuntimeError("durable fees exceed the configured paper BNB balance")
    open_quantity = Position.quantity - Position.closed_quantity
    async with session_scope() as session:
        result = await session.execute(
            select(
                Market.symbol,
                Market.market_type,
                Position.side,
                open_quantity,
                # Prorate the entry notional onto what is left, so gross
                # exposure matches the quantity actually being carried.
                Position.entry_notional_usd * open_quantity / Position.quantity,
                # Entry fees only: exit fees were paid out of the cash the
                # close returned, and charging them again here would deduct
                # them twice from the restored balance.
                Position.fees_usd - Position.exit_fees_usd,
            )
            .join(Market, Market.id == Position.market_id)
            .where(
                Position.mode == ExecutionMode.PAPER,
                Position.status.in_(LIVE_POSITION_STATUSES),
                Position.is_shadow.is_(False),
                open_quantity > 0,
            )
        )
        seeds = [PaperPositionSeed(*row) for row in result]
    account.restore(
        seeds,
        durable_cash_usd=cash,
        durable_bnb_balance=bnb_balance,
    )
    return account


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
    settings: Settings,
    dispatcher: ExecutionDispatcher | None = None,
    *,
    interval_seconds: float,
) -> None:
    """Evaluate every strategy against the latest snapshots, on a fixed cadence.

    Episodes are tracked here rather than in the recorder so that evaluation
    and persistence stay independent: without a database the strategy still
    runs and still draws, it simply keeps no record.

    Execution hangs off the same loop, after evaluation, and only ever sees
    what the strategy already validated - plus, if shadow probing is on, one
    deliberately chosen rejection per interval.
    """
    last_probe: datetime | None = None
    while True:
        runner.set_funding(funding.rates)
        now = datetime.now(UTC)
        evaluations = runner.evaluate(engine.snapshots(), monitor.metrics())
        if episodes is not None:
            ended = episodes.update(evaluations, now)
            if opportunities is not None:
                opportunities.record(ended)
            if dispatcher is not None:
                dispatcher.release({episode.uid for episode in ended})
        if dispatcher is not None and episodes is not None:
            last_probe = _enqueue_executions(
                evaluations,
                dispatcher,
                episodes,
                settings,
                now=now,
                last_probe=last_probe,
            )
        await asyncio.sleep(interval_seconds)


def _enqueue_executions(
    evaluations: list[StrategyEvaluation],
    dispatcher: ExecutionDispatcher,
    episodes: EpisodeTracker,
    settings: Settings,
    *,
    now: datetime,
    last_probe: datetime | None,
) -> datetime | None:
    """Simulate what the strategy validated, and optionally one probe.

    Returns when the last probe happened, so they stay spaced out rather than
    firing every evaluation cycle.
    """
    config = settings.execution
    if config.enabled:
        for evaluation in evaluations:
            for item in evaluation.actionable:
                if item.signal is None:  # pragma: no cover - actionable implies one
                    continue
                uid = episodes.uid_for(episode_key(evaluation.strategy, item))
                if uid is not None and dispatcher.enqueue(item.signal, uid, is_shadow=False):
                    episodes.mark_executed(episode_key(evaluation.strategy, item), item.signal)

    if not config.shadow:
        return last_probe
    due = last_probe is None or (now - last_probe) >= timedelta(
        milliseconds=config.shadow_interval_ms
    )
    if not due:
        return last_probe
    chosen = choose_probe(
        evaluations, allow_spot_short=settings.strategy.spot_perp_basis.allow_spot_short
    )
    if chosen is None:
        return last_probe
    evaluation, item = chosen
    signal = probe_signal(
        evaluation, item, now, timedelta(milliseconds=settings.execution.timeout_ms)
    )
    if signal is None:  # pragma: no cover - choose_probe guarantees a priced edge
        return last_probe
    uid = episodes.uid_for(episode_key(evaluation.strategy, item))
    if uid is None:
        return last_probe
    return now if dispatcher.enqueue(signal, uid, is_shadow=True) else last_probe


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
    attempts: Sequence[ExecutionAttempt] | None = None,
) -> None:
    tty = sys.stdout.isatty()
    interval = interval_seconds if tty else max(interval_seconds, _UNATTENDED_INTERVAL_SECONDS)
    while True:
        rows = max(5, shutil.get_terminal_size().lines - FRAME_OVERHEAD) if tty else None
        panels = _strategy_panels(
            runner, cost_summary, funding, episodes, opportunities, attempts, max_rows=rows
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
    attempts: Sequence[ExecutionAttempt] | None = None,
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
    panels = [
        render_evaluation(
            evaluation,
            cost_summary=cost_summary,
            max_rows=panel_rows,
            funding_unknown=sorted(unknown),
            recording=recording,
        )
        for evaluation in runner.evaluations()
    ]
    if attempts:
        panels.append(render_execution(attempts, max_rows=panel_rows))
    return panels


def _stop_on_signals() -> asyncio.Event:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        # Windows has no loop signal handlers; Ctrl-C still ends the run there.
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)
    return stop
