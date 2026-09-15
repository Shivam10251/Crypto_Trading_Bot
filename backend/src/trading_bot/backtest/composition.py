"""Wiring the real pipeline for one replay.

Every component below is the class the market-data service runs - strategy,
cost model, runner, episode tracker, opportunity recorder, paper account,
paper adapter, coordinator, dispatcher, risk engine, kill switch, portfolio
store, P&L source, closer, snapshot writer, portfolio service. Nothing is a
backtest variant of any of them. What differs is only what they are handed:

| Live service | Replay |
| --- | --- |
| ``MarketDataEngine`` | ``ReplayMarketData`` (same read API) |
| wall clock, ``asyncio.sleep`` | ``ReplayClock`` and its virtual ``sleep`` |
| ``session_scope`` | a session factory counted by the replay loop |
| ``ExecutionMode.PAPER``, no run | ``ExecutionMode.BACKTEST`` and this run's id |
| random episode uids, claim ids, kill-switch intent ids | derived from the configuration hash |
| funding polled over REST | recorded funding observations |
| venue average-price REST call | none: that filter is reported unsupported |
"""

from __future__ import annotations

import itertools
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from trading_bot.backtest.clock import ReplayClock
from trading_bot.backtest.funding import ReplayFundingAttributor, ReplayFundingLedger
from trading_bot.backtest.integrity import Probe, ReplayIntegrity
from trading_bot.backtest.loop import SessionFactory
from trading_bot.backtest.market_state import CarryLimits, ReplayMarketData
from trading_bot.backtest.runs import RunIdentity
from trading_bot.backtest.source import DatasetIssues
from trading_bot.core.config import Settings
from trading_bot.db.models.enums import ExecutionMode, OrderType
from trading_bot.db.scope import RunScope
from trading_bot.exchange.models import MarketRef, MarketSpec
from trading_bot.execution.account import PaperAccount, paper_account_for
from trading_bot.execution.coordinator import ExecutionCoordinator
from trading_bot.execution.dispatcher import ExecutionDispatcher
from trading_bot.execution.paper import PaperExecutionAdapter, Sleeper
from trading_bot.execution.recorder import ExecutionRecorder
from trading_bot.monitoring.monitor import MarketMonitor
from trading_bot.opportunities.episodes import EpisodeTracker, deterministic_uid
from trading_bot.opportunities.recorder import OpportunityRecorder
from trading_bot.portfolio.closer import PositionCloser
from trading_bot.portfolio.pnl_source import PortfolioPnlSource
from trading_bot.portfolio.service import PortfolioService
from trading_bot.portfolio.snapshots import SnapshotWriter
from trading_bot.portfolio.store import PortfolioStore
from trading_bot.portfolio.valuation import MarkReader
from trading_bot.risk.engine import RiskEngine
from trading_bot.risk.kill_switch import KillSwitchState
from trading_bot.risk.store import RiskEventStore
from trading_bot.strategy.base import StrategyContext
from trading_bot.strategy.costs import TransactionCostModel
from trading_bot.strategy.fees import FeeSchedule
from trading_bot.strategy.registry import build_strategies
from trading_bot.strategy.runner import StrategyRunner

#: Stable across runs, so identical inputs produce identical identities.
_NAMESPACE = uuid.UUID("5f1f3c2e-6b0a-4c7e-9a0e-4d8b8e2b9a11")
CLOSER_WORKER_ID = "backtest-closer"


@dataclass(slots=True)
class Pipeline:
    market: ReplayMarketData
    monitor: MarketMonitor
    runner: StrategyRunner
    episodes: EpisodeTracker
    opportunities: OpportunityRecorder
    account: PaperAccount
    adapter: PaperExecutionAdapter
    executions: ExecutionRecorder
    risk_events: RiskEventStore
    kill_switch: KillSwitchState
    risk: RiskEngine
    dispatcher: ExecutionDispatcher | None
    store: PortfolioStore
    pnl_source: PortfolioPnlSource
    closer: PositionCloser | None
    portfolio: PortfolioService
    funding: ReplayFundingAttributor
    funding_ledger: ReplayFundingLedger
    writer: SnapshotWriter


def identity_namespace(config_hash: str) -> uuid.UUID:
    """Episode uids depend on the configuration, never on the run's own id."""
    return uuid.uuid5(_NAMESPACE, config_hash)


def build_pipeline(
    settings: Settings,
    *,
    run: RunIdentity,
    refs: tuple[MarketRef, ...],
    specs: dict[MarketRef, MarketSpec],
    market_ids: dict[MarketRef, int],
    clock: ReplayClock,
    sleep: Sleeper,
    session_factory: SessionFactory,
    issues: DatasetIssues,
) -> Pipeline:
    scope = RunScope.backtest(run.id)
    market = ReplayMarketData(
        refs,
        clock,
        CarryLimits(
            depth_levels=settings.market_data.depth_levels,
            market_silence_ms=settings.market_data.market_silence_ms,
            max_book_carry_ms=settings.backtest.max_book_carry_ms,
            max_funding_carry_ms=settings.backtest.max_funding_carry_ms,
            liquidity_band_bps=Decimal(str(settings.market_data.liquidity_band_bps)),
            reference_notional=Decimal(str(settings.market_data.reference_order_notional)),
        ),
        issues,
    )
    monitor = MarketMonitor(market, settings.monitoring, clock=clock)
    cost_model = TransactionCostModel(settings.costs, specs=specs)
    runner = StrategyRunner(
        build_strategies(settings.strategy),
        StrategyContext(cost_model=cost_model, specs=specs),
        specs=specs,
        clock=clock,
    )
    episodes = EpisodeTracker(uid_factory=deterministic_uid(identity_namespace(run.config_hash)))
    opportunities = OpportunityRecorder(
        market_ids,
        session_factory,
        interval_seconds=settings.backtest.flush_interval_ms / 1000,
        min_duration_ms=settings.opportunities.min_duration_ms,
        scope=scope,
    )
    account = paper_account_for(settings)
    adapter = PaperExecutionAdapter(
        market,
        settings.execution,
        fees=FeeSchedule.from_config(settings.costs),
        specs=lambda ref: specs.get(ref),
        mark_prices=market.mark_price,
        average_prices=None,
        clock=clock,
        sleep=sleep,
        mode=ExecutionMode.BACKTEST,
    )
    coordinator = ExecutionCoordinator(
        adapter,
        order_type=OrderType(settings.execution.entry_order_type.upper()),
        specs=lambda ref: specs.get(ref),
        allow_spot_short=settings.strategy.spot_perp_basis.allow_spot_short,
        max_leg_skew_ms=settings.execution.max_leg_skew_ms,
        clock=clock,
        account=account,
        propagate_adapter_errors=True,
    )
    executions = ExecutionRecorder(
        market_ids,
        session_factory,
        interval_seconds=settings.backtest.flush_interval_ms / 1000,
        backtest_run_id=run.id,
    )
    risk_events = RiskEventStore(session_factory, backtest_run_id=run.id)
    transitions = itertools.count()
    kill_switch = KillSwitchState(
        risk_events,
        session_factory,
        mode=ExecutionMode.BACKTEST,
        clock=clock,
        backtest_run_id=run.id,
        intent_ids=lambda: f"kill_switch:{next(transitions)}",
    )
    refs_by_id = {market_id: ref for ref, market_id in market_ids.items()}
    funding = ReplayFundingAttributor(
        market,
        refs_by_id,
        max_observation_age=timedelta(milliseconds=settings.backtest.funding_settlement_max_age_ms),
    )
    funding_ledger = ReplayFundingLedger(
        market,
        refs_by_id,
        session_factory,
        scope,
        account,
        max_observation_age=timedelta(milliseconds=settings.backtest.funding_settlement_max_age_ms),
    )
    store = PortfolioStore(
        session_factory, mode=ExecutionMode.BACKTEST, backtest_run_id=run.id, funding=funding
    )
    pnl_source = PortfolioPnlSource(
        store,
        venue=settings.exchange.venue,
        streak_limit=settings.risk.max_consecutive_losses,
        refresh_interval=timedelta(milliseconds=settings.portfolio.pnl_refresh_ms),
        clock=clock,
    )
    risk = RiskEngine(
        settings.risk,
        account,
        risk_events,
        kill_switch,
        pnl_source=pnl_source,
        mode=ExecutionMode.BACKTEST,
        clock=clock,
    )
    dispatcher = (
        ExecutionDispatcher(
            coordinator,
            executions,
            queue_size=settings.execution.queue_size,
            workers=settings.execution.workers,
            recent_attempts=settings.execution.recent_attempts,
            timeout_ms=settings.execution.timeout_ms,
            risk_engine=risk,
        )
        if settings.backtest.execution_enabled
        else None
    )
    if dispatcher is not None:
        kill_switch.add_listener(dispatcher.purge)
    marks = MarkReader(market, max_book_age_ms=settings.portfolio.mark_max_age_ms)
    writer = SnapshotWriter(
        store,
        session_factory,
        initial_cash_usd=Decimal(str(settings.execution.paper_cash_usd)),
        fees_paid_in_cash=not settings.costs.pay_fees_in_bnb,
        interval=timedelta(milliseconds=settings.portfolio.snapshot_interval_ms),
        min_return_observations=settings.portfolio.min_return_observations,
        risk_free_rate_annual_pct=settings.portfolio.risk_free_rate_annual_pct,
    )
    closer = (
        PositionCloser(
            store=store,
            session_factory=session_factory,
            adapter=adapter,
            risk=risk,
            marks=marks,
            account=account,
            config=settings.portfolio.exits,
            venue=settings.exchange.venue,
            mode=ExecutionMode.BACKTEST,
            clock=clock,
            worker_id=CLOSER_WORKER_ID,
        )
        if settings.backtest.execution_enabled and settings.backtest.exits_enabled
        else None
    )
    portfolio = PortfolioService(
        store=store,
        writer=writer,
        marks=marks,
        closer=closer,
        pnl_source=pnl_source,
        config=settings.portfolio,
        venue=settings.exchange.venue,
        clock=clock,
        # Replay is a single writer, so snapshots fold history in rather than
        # re-reading it - measured quadratic otherwise (portfolio.incremental).
        incremental=True,
    )
    return Pipeline(
        market=market,
        monitor=monitor,
        runner=runner,
        episodes=episodes,
        opportunities=opportunities,
        account=account,
        adapter=adapter,
        executions=executions,
        risk_events=risk_events,
        kill_switch=kill_switch,
        risk=risk,
        dispatcher=dispatcher,
        store=store,
        pnl_source=pnl_source,
        closer=closer,
        portfolio=portfolio,
        funding=funding,
        funding_ledger=funding_ledger,
        writer=writer,
    )


def integrity_for(pipeline: Pipeline, *, attempts: int) -> ReplayIntegrity:
    """Every counter through which a live component reports a swallowed failure."""
    market, executions = pipeline.market, pipeline.executions
    opportunities, risk_events = pipeline.opportunities, pipeline.risk_events
    probes = [
        Probe(
            "replay invariant violations",
            lambda: len(market.violations),
            lambda: market.violations[-1] if market.violations else None,
        ),
        Probe("execution records dropped", lambda: executions.dropped),
        Probe("execution records for unregistered markets", lambda: executions.unregistered),
        Probe("opportunity records dropped", lambda: opportunities.dropped),
        Probe("opportunity records for unregistered markets", lambda: opportunities.unregistered),
        Probe(
            "risk decisions that could not be stored",
            lambda: risk_events.persist_failures,
            lambda: risk_events.last_error,
        ),
        Probe("risk events dropped", lambda: risk_events.queued_dropped),
        Probe("kill-switch state read failures", lambda: pipeline.kill_switch.refresh_failures),
        Probe("kill-switch listener failures", lambda: pipeline.kill_switch.listener_failures),
        Probe("P&L view read failures", lambda: pipeline.pnl_source.read_failures),
    ]
    closer = pipeline.closer
    if closer is not None:
        probes.append(
            Probe("exit sweep failures", lambda: closer.failures, lambda: closer.last_error)
        )
    return ReplayIntegrity(
        probes=probes,
        queues={
            "orders, fills and positions": executions,
            "opportunities and signals": opportunities,
            "risk events": risk_events,
        },
        attempts=attempts,
    )


def next_tick_after(start: datetime, interval: timedelta, now: datetime) -> datetime:
    """The first tick of a grid anchored at ``start`` strictly after ``now``."""
    if now < start:
        return start
    steps = (now - start) // interval + 1
    return start + steps * interval
