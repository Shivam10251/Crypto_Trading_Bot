"""The replay driver: recorded history through the real pipeline, in virtual time.

```
HistoricalDataSource (one snapshot) ──▶ EventValidator ──▶ ReplayMarketData (gated by the clock)
      initial state ─────────────────────────┘                  │ snapshot / execution_snapshot
    ReplayClock ── ticks on fixed grids ──▶ monitor ──▶ portfolio snapshot ──▶ exits
                                                   ──▶ strategy + execution ──▶ flush
```

**One flow at a time.** The live service runs its loops concurrently; replay
runs the same steps one after another at each virtual instant, awaiting each
to completion: concurrent database work finishes in whatever order the network
allows, and a replay that depends on that is not reproducible. Both legs of an
execution still sleep their latencies concurrently. The serialization is a
declared execution-model limitation of every run (``completeness``), not a
parity claim: no queue, no ``QUEUE_OVERLOAD``, no overlapping attempts.

**Tick order at one instant**: monitor sample, portfolio snapshot, exit
sweep, strategy evaluation and the executions it admits, flush. The snapshot
comes first, so a snapshot stamped at an instant values the book as it stood
*at* that instant, before anything decided there - and the first one, at the
start, is the run's equity baseline before any strategy execution. A trade on
the very first evaluation therefore shows its immediate cost in the second
point of the curve, never hidden inside the first. Snapshots run on the
portfolio's epoch-aligned grid (the baseline is stamped with its grid floor;
a fresh account holds nothing then, so its value is exactly its cash), other
steps on grids anchored at the start. A step that moves the clock - an
order's latency - makes every later step at that instant run at the later
time; a tick whose instant was overtaken runs once, at the first moment it
can.

**The end.** The clock advances to the requested end, every remaining event is
applied, open episodes close, and a terminal snapshot values the book at the
end. On a grid point it is an ordinary snapshot; between grid points it is
stamped at the end instant itself, because the grid floor already holds a
published valuation a second write would overwrite; that point counts for
final equity and drawdown but not for return series. Open positions are
valued, never force-closed at the last book. Every record is then flushed
strictly and the paper account reconciled against the durable rows.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from trading_bot.backtest.clock import ReplayClock, ReplayInvariantError
from trading_bot.backtest.completeness import (
    Completeness,
    assess_pipeline,
    coverage_warnings,
    failed_verdict,
)
from trading_bot.backtest.composition import (
    Pipeline,
    build_pipeline,
    integrity_for,
    next_tick_after,
)
from trading_bot.backtest.feeder import Feeder, HeartbeatTask, arrival_sleep, open_replay
from trading_bot.backtest.integrity import (
    ReplayIntegrity,
    durable_counts,
    flush_best_effort,
    reconcile,
)
from trading_bot.backtest.loop import SessionFactory
from trading_bot.backtest.runs import RunIdentity, RunProgress, RunStore
from trading_bot.backtest.source import (
    DatasetCoverage,
    DatasetRequest,
    EventValidator,
    HistoricalDataSource,
    TemporalLimits,
)
from trading_bot.core.config import Settings
from trading_bot.core.logging import get_logger
from trading_bot.db.models.enums import BacktestRunStatus
from trading_bot.db.scope import RunScope
from trading_bot.opportunities.episodes import episode_key
from trading_bot.portfolio.snapshots import floor_to

logger = get_logger(__name__)

TICK_ORDER = ("monitor", "snapshot", "exits", "evaluate", "flush")
_FAR_FUTURE = timedelta(days=36_500)


@dataclass(slots=True)
class BacktestOutcome:
    run: RunIdentity
    status: BacktestRunStatus
    progress: RunProgress
    warnings: list[str] = field(default_factory=list)
    completeness: dict[str, object] | None = None
    dataset_issues: dict[str, object] | None = None
    failure_reason: str | None = None
    fingerprint: str | None = None
    real_seconds: float = 0.0
    virtual_seconds: float = 0.0


@dataclass(slots=True)
class _Valuation:
    captured_at: datetime | None = None
    valued_at: datetime | None = None
    status: str | None = None


class BacktestEngine:
    def __init__(
        self,
        settings: Settings,
        *,
        source: HistoricalDataSource,
        session_factory: SessionFactory,
        runs: RunStore,
        request: DatasetRequest,
        clock: ReplayClock,
        wall_clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._settings = settings
        self._source = source
        self._session_factory = session_factory
        self._runs = runs
        self._request = request
        self._clock = clock
        self._wall = wall_clock
        self._cancel_requested = False
        self._lost = False
        self._valuation = _Valuation()
        self._feeder: Feeder | None = None

    def request_cancel(self) -> None:
        """Stop at the next tick boundary, as a durable cancel request would."""
        self._cancel_requested = True

    def mark_lost(self) -> None:
        """Another process finished this run's row; stop without overwriting it."""
        self._lost = True

    async def run(self, run: RunIdentity) -> BacktestOutcome:
        started = self._wall()
        progress = RunProgress()
        settings = self._settings.backtest
        validator = EventValidator(
            self._request,
            gap_ms=settings.gap_warning_ms,
            # Funding is polled, not streamed: a gap is two missed polls.
            funding_gap_ms=max(
                settings.gap_warning_ms,
                int(2_000 * self._settings.strategy.spot_perp_basis.funding_refresh_seconds),
            ),
            temporal=TemporalLimits(
                max_clock_skew_ms=settings.max_clock_skew_ms,
                max_exchange_lag_ms=settings.max_exchange_lag_ms,
                funding_schedule_tolerance_ms=settings.funding_schedule_tolerance_ms,
            ),
        )
        outcome = BacktestOutcome(run=run, status=BacktestRunStatus.FAILED, progress=progress)
        coverage: DatasetCoverage | None = None
        pipeline: Pipeline | None = None
        heartbeat: HeartbeatTask | None = None
        try:
            if not await self._runs.mark_running(run.id):
                outcome.status = BacktestRunStatus.CANCELLED
                outcome.failure_reason = "cancelled before it started"
            else:
                heartbeat = HeartbeatTask(
                    self._runs,
                    run.id,
                    progress,
                    interval_seconds=settings.heartbeat_seconds,
                    on_cancel=self.request_cancel,
                    on_lost=self.mark_lost,
                )
                heartbeat.start()
                await self._source.open()
                coverage = await self._source.coverage(self._request)
                validator.record_specs(coverage.specs, coverage.spec_versions)
                validator.record_capture_gaps(coverage.capture_gaps)
                pipeline = self._build(run, coverage, validator)
                integrity = integrity_for(pipeline, attempts=settings.persistence_attempts)
                # The state in force at the start, then the in-window stream.
                feeder = self._feeder = await open_replay(
                    self._source, self._request, self._settings, pipeline.market, validator
                )
                if coverage.events == 0 and validator.initialization_events == 0:
                    raise _NoData("the source holds no recorded events for this request")
                await pipeline.kill_switch.load()
                integrity.check()
                outcome.status = await self._replay(pipeline, feeder, progress, integrity)
                if outcome.status is BacktestRunStatus.CANCELLED:
                    outcome.failure_reason = "cancelled on request"
                    await self._flush_best_effort(pipeline, progress)
                elif outcome.status is BacktestRunStatus.FAILED:
                    outcome.failure_reason = "another process finished this run's row"
                else:
                    await self._finish(pipeline, feeder, progress, integrity)
                    validator.finish(self._request.refs)
                    verdict = await self._completeness(run, pipeline, coverage, validator)
                    outcome.status = verdict.status()
                    outcome.completeness = verdict.as_dict()
                    outcome.fingerprint = validator.fingerprint.hexdigest()
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is not None:
                current.uncancel()
            outcome.status = BacktestRunStatus.CANCELLED
            outcome.failure_reason = "interrupted"
            if pipeline is not None:
                await self._flush_best_effort(pipeline, progress)
        except _NoData as exc:
            outcome.status = BacktestRunStatus.FAILED
            outcome.failure_reason = str(exc)
        except Exception as exc:
            logger.exception("backtest.failed", run=str(run.run_uid))
            outcome.status = BacktestRunStatus.FAILED
            outcome.failure_reason = f"{type(exc).__name__}: {exc}"
            if pipeline is not None:
                await self._flush_best_effort(pipeline, progress)
        finally:
            if heartbeat is not None:
                await heartbeat.stop()
            try:
                await self._source.close()
            except Exception:
                logger.exception("backtest.source_close_failed")

        if outcome.status not in (BacktestRunStatus.COMPLETED, BacktestRunStatus.INCOMPLETE):
            # A run that did not finish claims no dataset and no verdict.
            outcome.fingerprint = None
            outcome.completeness = (
                failed_verdict(outcome.failure_reason or "")
                if outcome.status is BacktestRunStatus.FAILED
                else None
            )
        progress.initialization_events = validator.initialization_events
        progress.events_accepted = validator.events_accepted
        progress.events_rejected = validator.events_rejected
        warnings = coverage_warnings(coverage) if coverage is not None else []
        warnings += [
            f"dataset: {count} {kind.replace('_', ' ')} event(s)"
            for kind, count in sorted(validator.issues.counts.items())
        ]
        outcome.warnings = warnings[: settings.max_warnings]
        outcome.dataset_issues = validator.issues.as_dict()
        outcome.real_seconds = self._wall() - started
        outcome.virtual_seconds = (self._clock.now() - self._request.start).total_seconds()
        market = pipeline.market if pipeline is not None else None
        stored = await self._runs.finish(
            run.id,
            outcome.status,
            progress=progress,
            actual_start=market.first_applied_at if market else None,
            actual_end=market.last_applied_at if market else None,
            fingerprint=outcome.fingerprint,
            warnings=outcome.warnings,
            dataset_issues=outcome.dataset_issues,
            completeness=outcome.completeness,
            failure_reason=(
                outcome.failure_reason if outcome.status is BacktestRunStatus.FAILED else None
            ),
        )
        if stored is not outcome.status:
            logger.warning(
                "backtest.terminal_state_kept",
                run=str(run.run_uid),
                computed=outcome.status.value,
                stored=stored.value,
            )
            outcome.status = stored
        logger.info(
            "backtest.finished",
            run=str(run.run_uid),
            status=outcome.status.value,
            events=progress.events_replayed,
            orders=progress.orders_recorded,
            trades=progress.trades_completed,
            real_seconds=round(outcome.real_seconds, 3),
            virtual_seconds=round(outcome.virtual_seconds, 3),
        )
        return outcome

    # --- setup ----------------------------------------------------------

    def _build(
        self, run: RunIdentity, coverage: DatasetCoverage, validator: EventValidator
    ) -> Pipeline:
        specs = {ref: coverage.specs[ref] for ref in self._request.refs if ref in coverage.specs}
        return build_pipeline(
            self._settings,
            run=run,
            refs=tuple(ref for ref in self._request.refs if ref in specs),
            specs=specs,
            market_ids=self._source.market_ids,
            clock=self._clock,
            sleep=arrival_sleep(self._clock, lambda: self._feeder),
            session_factory=self._session_factory,
            issues=validator.issues,
        )

    # --- the loop -------------------------------------------------------

    async def _replay(
        self,
        pipeline: Pipeline,
        feeder: Feeder,
        progress: RunProgress,
        integrity: ReplayIntegrity,
    ) -> BacktestRunStatus:
        settings = self._settings
        start, end = self._request.start, self._request.end
        intervals = {
            "monitor": timedelta(milliseconds=settings.monitoring.sample_interval_ms),
            "snapshot": timedelta(milliseconds=settings.portfolio.snapshot_interval_ms),
            "evaluate": timedelta(milliseconds=settings.strategy.evaluate_interval_ms),
            "flush": timedelta(milliseconds=settings.backtest.flush_interval_ms),
        }
        if pipeline.closer is not None:
            intervals["exits"] = timedelta(
                milliseconds=settings.portfolio.exits.evaluate_interval_ms
            )
        anchors = dict.fromkeys(intervals, start)
        anchors["snapshot"] = floor_to(start, intervals["snapshot"])
        due = dict.fromkeys(intervals, start)
        while True:
            if self._lost:
                return BacktestRunStatus.FAILED
            if self._cancel_requested:
                return BacktestRunStatus.CANCELLED
            moment = min(due.values())
            if moment >= end:
                return BacktestRunStatus.COMPLETED
            await feeder.ensure_covers(moment)
            self._clock.advance_to(max(moment, self._clock.now()))
            # Funding is a cash flow at the settlement instant. Apply every
            # payment due before any observer at this virtual time reads cash,
            # equity, margin or daily loss.
            await pipeline.funding_ledger.settle_due(self._clock.now())
            for name in TICK_ORDER:
                if name in due and due[name] <= self._clock.now():
                    await self._tick(name, pipeline, progress, integrity)
                    integrity.check()
                    due[name] = next_tick_after(anchors[name], intervals[name], self._clock.now())
            progress.events_replayed = pipeline.market.events_applied

    async def _tick(
        self, name: str, pipeline: Pipeline, progress: RunProgress, integrity: ReplayIntegrity
    ) -> None:
        if name == "monitor":
            pipeline.monitor.sample()
        elif name == "snapshot":
            await self._snapshot(pipeline)
        elif name == "exits" and pipeline.closer is not None:
            # Measured: the sweep's live-attempts query was about two thirds of
            # replay time, almost all of it on a flat book. In a single-writer
            # replay the account holds gross exposure exactly while the run has
            # a live position - entries settle into it before they are
            # flushed, exits after they are recorded - so a flat account has
            # nothing to sweep.
            if pipeline.account.gross_exposure_usd > 0:
                await pipeline.closer.sweep()
        elif name == "evaluate":
            await self._evaluate(pipeline, progress, integrity)
        elif name == "flush":
            await integrity.flush()
            progress.opportunities_recorded = pipeline.opportunities.opportunities_written

    async def _snapshot(self, pipeline: Pipeline, *, at_instant: bool = False) -> None:
        valued_at = self._clock.now()
        state = await pipeline.portfolio.snapshot(at_instant=at_instant)
        previous = self._valuation.captured_at
        if previous is not None and state.captured_at <= previous:
            raise ReplayInvariantError(
                f"snapshot at {state.captured_at.isoformat()} does not follow "
                f"{previous.isoformat()}; it would overwrite a published valuation"
            )
        self._valuation = _Valuation(state.captured_at, valued_at, state.valuation_status.value)

    async def _evaluate(
        self, pipeline: Pipeline, progress: RunProgress, integrity: ReplayIntegrity
    ) -> None:
        """The market-data service's strategy step, without its sleep or probes."""
        pipeline.runner.set_funding(pipeline.market.rates)
        now = self._clock.now()
        evaluations = pipeline.runner.evaluate(
            pipeline.market.snapshots(), pipeline.monitor.metrics()
        )
        progress.evaluations += 1
        ended = pipeline.episodes.update(evaluations, now)
        pipeline.opportunities.record(ended)
        dispatcher = pipeline.dispatcher
        if dispatcher is None:
            return
        dispatcher.release({episode.uid for episode in ended})
        for evaluation in evaluations:
            for item in evaluation.actionable:
                if item.signal is None:  # pragma: no cover - actionable implies one
                    continue
                key = episode_key(evaluation.strategy, item)
                uid = pipeline.episodes.uid_for(key)
                if uid is None:
                    continue
                # An unexpected exception inside propagates and fails the run;
                # the episode is then never marked executed.
                if await dispatcher.execute_inline(item.signal, uid, is_shadow=False):
                    pipeline.episodes.mark_executed(key, item.signal)
                    # Written now, strictly: the next exit sweep reads them.
                    await integrity.flush()
                    progress.orders_recorded = pipeline.executions.orders_written
                    progress.fills_recorded = pipeline.executions.fills_written
                # Order latency may have moved virtual time across a funding
                # boundary. Settle before the next signal can reserve capital.
                await pipeline.funding_ledger.settle_due(self._clock.now())

    # --- the end --------------------------------------------------------

    async def _finish(
        self,
        pipeline: Pipeline,
        feeder: Feeder,
        progress: RunProgress,
        integrity: ReplayIntegrity,
    ) -> None:
        end = max(self._request.end, self._clock.now())
        await feeder.ensure_covers(end)
        self._clock.advance_to(end)
        pipeline.market.catch_up()
        await pipeline.funding_ledger.settle_due(end)
        pipeline.opportunities.record(pipeline.episodes.close_all())
        interval = timedelta(milliseconds=self._settings.portfolio.snapshot_interval_ms)
        last = self._valuation.captured_at
        if last is None or floor_to(end, interval) > last:
            await self._snapshot(pipeline)
        elif end > last:
            # The end lies between grid points and the floor already holds a
            # published valuation: value the end at its own instant instead.
            await self._snapshot(pipeline, at_instant=True)
        await integrity.flush()
        await reconcile(
            pipeline.account,
            session_factory=self._session_factory,
            scope=pipeline.writer.scope,
            durable_balances=pipeline.writer.balances,
            bnb_price_usd=self._bnb_price(),
            initial_bnb=Decimal(str(self._settings.execution.paper_bnb_balance)),
        )
        progress.events_replayed = pipeline.market.events_applied
        progress.opportunities_recorded = pipeline.opportunities.opportunities_written
        # Durable totals: exits write their own orders and fills, not through
        # the execution recorder its counters describe.
        progress.orders_recorded, progress.fills_recorded = await durable_counts(
            self._session_factory, pipeline.writer.scope
        )
        progress.trades_completed = len(
            await pipeline.store.attempts_closed_between(
                self._settings.exchange.venue, None, self._request.end + _FAR_FUTURE
            )
        )

    async def _flush_best_effort(self, pipeline: Pipeline, progress: RunProgress) -> None:
        """What a failed or cancelled run recorded, kept for diagnosis only."""
        pipeline.opportunities.record(pipeline.episodes.close_all())
        await flush_best_effort(pipeline.executions, pipeline.opportunities, pipeline.risk_events)
        progress.events_replayed = pipeline.market.events_applied
        progress.opportunities_recorded = pipeline.opportunities.opportunities_written
        progress.orders_recorded = pipeline.executions.orders_written
        progress.fills_recorded = pipeline.executions.fills_written

    # --- honesty --------------------------------------------------------

    async def _completeness(
        self,
        run: RunIdentity,
        pipeline: Pipeline,
        coverage: DatasetCoverage,
        validator: EventValidator,
    ) -> Completeness:
        return await assess_pipeline(
            pipeline,
            coverage,
            validator.issues,
            venue=self._settings.exchange.venue,
            end=self._clock.now(),
            valuation_status=self._valuation.status,
            valued_at=self._valuation.valued_at,
            session_factory=self._session_factory,
            scope=RunScope.backtest(run.id),
        )

    def _bnb_price(self) -> Decimal | None:
        price = self._settings.execution.paper_bnb_price_usd
        if not self._settings.costs.pay_fees_in_bnb or price is None:
            return None
        return Decimal(str(price))


class _NoData(Exception):
    """Nothing to replay; the run fails rather than completing on nothing."""
