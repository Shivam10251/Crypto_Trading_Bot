"""What a finished run can and cannot claim, component by component.

One flag cannot say it. A run can replay every recorded event and still omit
funding from its P&L; it can account for every cash flow and still have no
book at its last valuation; and every PostgreSQL replay executes under a
model that cannot check filters that were never stored. Each is a different
statement, so each is its own component:

- ``dataset``: every requested market has reference data and its quote,
  depth and (perpetual) funding streams - in range or in force at the start -
  and nothing was corrupt, out of order, temporally invalid, or carried past
  its limit. Otherwise the run is ``INCOMPLETE``.
- ``accounting``: no trade, open or closed, is missing a cash flow (funding,
  spot borrow). A perpetual leg still open at the end has no attributed
  funding; it counts as measured only if no settlement fell while it was
  held (then none was charged). Otherwise ``INCOMPLETE``.
- ``valuation``: the last snapshot valued every open position, and nothing
  filled after it. Otherwise ``INCOMPLETE``.
- ``persistence``: every record was written. Otherwise the run is ``FAILED``
  and carries no verdict at all.
- ``execution_model``: never complete for this engine - see ``limitations``.
  It sets fidelity, not status.

**Execution-model limitations are declared fidelity, not incompleteness.**
They are properties of *every* run of a source and configuration - venue
filters the ``markets`` table never stored, sampled depth, serialized
scheduling - so making them ``INCOMPLETE`` would make ``INCOMPLETE`` mean
nothing. They are listed on every run and every report, and a comparison
between runs is only meaningful between runs that declare the same list.

``performance_rankable`` is true only for ``COMPLETED``: a zero-trade run is
rankable when its data was complete and not when the data was missing.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select

from trading_bot.backtest.composition import Pipeline
from trading_bot.backtest.loop import SessionFactory
from trading_bot.backtest.source import TEMPORAL_ISSUES, DatasetCoverage, DatasetIssues
from trading_bot.db.models import Fill
from trading_bot.db.models.enums import BacktestRunStatus, MarketType, ValuationStatus
from trading_bot.db.scope import RunScope
from trading_bot.portfolio.accounting import FUNDING, SPOT_BORROW
from trading_bot.portfolio.records import AttemptRecord, LegRecord

#: Dataset issue kinds that make the dataset incomplete. A gap can suppress
#: opportunities by leaving the strategy on stale state, so it is not safe to
#: rank the resulting performance as though inactivity were market reality.
#: Duplicates and regressions remain counted but do not imply lost information.
INCOMPLETE_DATASET_ISSUES = (
    "capture_gap",
    "missing_stream",
    "corrupt",
    "out_of_order",
    "gap",
    "book_carry_expired",
    "funding_carry_expired",
    *TEMPORAL_ISSUES,
)

#: Always true of a replay through this engine, whatever the data.
SERIALIZED_SCHEDULING = (
    "serialized_scheduling: evaluation, exits, snapshots and executions run one at a time "
    "at each virtual instant; there is no execution queue, so QUEUE_OVERLOAD cannot occur, "
    "no two attempts overlap, and a signal due during an execution is evaluated after it"
)
SAMPLED_DEPTH = (
    "sampled_depth: a fill walks the latest captured book at its arrival, not the venue's "
    "continuous book; queue position and maker fills are not simulated"
)


@dataclass(slots=True)
class Completeness:
    dataset_missing: list[str] = field(default_factory=list)
    dataset_issues: dict[str, int] = field(default_factory=dict)
    dataset_consistency: str | None = None
    unmeasured: list[str] = field(default_factory=list)
    valuation_status: str | None = None
    valuation_as_of: datetime | None = None
    fills_after_valuation: int = 0
    open_positions: int = 0
    limitations: list[str] = field(default_factory=list)

    @property
    def dataset_complete(self) -> bool:
        return not self.dataset_missing and not self.dataset_issues

    @property
    def accounting_complete(self) -> bool:
        return not self.unmeasured

    @property
    def valuation_complete(self) -> bool:
        return (
            self.valuation_status == ValuationStatus.COMPLETE.value
            and self.fills_after_valuation == 0
        )

    def status(self) -> BacktestRunStatus:
        if self.dataset_complete and self.accounting_complete and self.valuation_complete:
            return BacktestRunStatus.COMPLETED
        return BacktestRunStatus.INCOMPLETE

    def as_dict(self) -> dict[str, Any]:
        status = self.status()
        return {
            "dataset": {
                "complete": self.dataset_complete,
                "missing": sorted(self.dataset_missing),
                "issues": dict(sorted(self.dataset_issues.items())),
                "consistency": self.dataset_consistency,
            },
            "execution_model": {"complete": False, "limitations": list(self.limitations)},
            "accounting": {"complete": self.accounting_complete, "unmeasured": self.unmeasured},
            "valuation": {
                "complete": self.valuation_complete,
                "status": self.valuation_status,
                "as_of": self.valuation_as_of.isoformat() if self.valuation_as_of else None,
                "fills_after_valuation": self.fills_after_valuation,
                "open_positions": self.open_positions,
            },
            "persistence": {"complete": True},
            "performance_rankable": status is BacktestRunStatus.COMPLETED,
        }


def dataset_verdict(
    coverage: DatasetCoverage, issues: DatasetIssues
) -> tuple[list[str], dict[str, int]]:
    """Streams absent from the range *and* from initialization are missing.

    Read from the validator's ``missing_stream`` issues, which already count
    an initialization observation as present: a 20-second run between two
    30-second funding polls has funding.
    """
    missing: set[str] = {f"market_reference_data:{ref}" for ref in coverage.unknown}
    missing.update(issues.samples.get("missing_stream", []))
    counted = {
        kind: count for kind, count in issues.counts.items() if kind in INCOMPLETE_DATASET_ISSUES
    }
    return sorted(missing), counted


def failed_verdict(reason: str) -> dict[str, Any]:
    """A failed run claims nothing but why it failed."""
    return {"persistence_or_runtime_failure": reason, "performance_rankable": False}


def limitations(unsupported_filters: Iterable[str]) -> list[str]:
    filters = list(unsupported_filters)
    listed = [SERIALIZED_SCHEDULING, SAMPLED_DEPTH]
    if filters:
        listed.append("venue_filters_unreproducible: " + "; ".join(filters))
    return listed


def component_names(completeness: Mapping[str, Any] | None) -> list[str]:
    """The incomplete components of a stored verdict, for a one-line summary."""
    if not completeness or "dataset" not in completeness:
        return []
    return [
        name
        for name in ("dataset", "accounting", "valuation")
        if not completeness.get(name, {}).get("complete", False)
    ]


async def assess(
    coverage: DatasetCoverage,
    issues: DatasetIssues,
    *,
    closed_attempts: Sequence[AttemptRecord],
    open_attempts: Sequence[AttemptRecord],
    no_settlement_while_open: Callable[[LegRecord], bool],
    valuation_status: str | None,
    valued_at: datetime | None,
    session_factory: SessionFactory,
    scope: RunScope,
) -> Completeness:
    """The verdict of a run that reached its end with every record written."""
    missing, counted = dataset_verdict(coverage, issues)
    components: set[str] = set()
    for attempt in closed_attempts:
        components.update(attempt.paired().unmeasured)
    for attempt in open_attempts:
        for leg in attempt.legs:
            missing_here = set(leg.accounting().unmeasured)
            # An open perpetual leg has no final position attribution. Replay
            # can nevertheless prove its funding complete to date when every
            # settlement crossed was observed (including the zero-settlement
            # case).
            if FUNDING in missing_here and no_settlement_while_open(leg):
                missing_here.discard(FUNDING)
            components.update(missing_here)
    names = {FUNDING: "funding", SPOT_BORROW: "spot_borrow"}
    unmeasured = sorted(names.get(component, component) for component in components)
    fills_after = 0
    if valued_at is not None:
        async with session_factory() as session:
            fills_after = int(
                await session.scalar(
                    select(func.count(Fill.id)).where(
                        *scope.filters(Fill.mode, Fill.backtest_run_id),
                        Fill.filled_at > valued_at,
                    )
                )
                or 0
            )
    return Completeness(
        dataset_missing=missing,
        dataset_issues=counted,
        dataset_consistency=coverage.consistency,
        unmeasured=unmeasured,
        valuation_status=valuation_status,
        valuation_as_of=valued_at,
        fills_after_valuation=fills_after,
        open_positions=sum(len(attempt.live_legs) for attempt in open_attempts),
        limitations=limitations(coverage.unsupported_filters),
    )


async def assess_pipeline(
    pipeline: Pipeline,
    coverage: DatasetCoverage,
    issues: DatasetIssues,
    *,
    venue: str,
    end: datetime,
    valuation_status: str | None,
    valued_at: datetime | None,
    session_factory: SessionFactory,
    scope: RunScope,
) -> Completeness:
    """``assess`` over a finished pipeline's own rows and replayed funding schedule."""
    closed = await pipeline.store.attempts_closed_between(venue, None, end + timedelta(days=36_500))
    live = await pipeline.store.live_attempts(venue)

    def no_settlement(leg: LegRecord) -> bool:
        return pipeline.funding.while_open(leg, end).amount_usd is not None

    return await assess(
        coverage,
        issues,
        closed_attempts=closed,
        open_attempts=live,
        no_settlement_while_open=no_settlement,
        valuation_status=valuation_status,
        valued_at=valued_at,
        session_factory=session_factory,
        scope=scope,
    )


def coverage_warnings(coverage: DatasetCoverage) -> list[str]:
    warnings = [f"no reference data for requested market {ref}" for ref in coverage.unknown]
    for market in coverage.markets:
        if market.books == 0:
            warnings.append(
                f"{market.ref}: no in-range order-book updates - replay can trade only while a "
                "valid pre-start snapshot remains fresh"
            )
        if market.ref.market_type is not MarketType.SPOT and market.funding == 0:
            warnings.append(
                f"{market.ref}: no in-range funding updates - pricing and attribution rely on "
                "a valid pre-start observation remaining fresh"
            )
        if market.quotes == 0:
            warnings.append(
                f"{market.ref}: no in-range quote updates - replay relies on valid pre-start state"
            )
    return warnings
