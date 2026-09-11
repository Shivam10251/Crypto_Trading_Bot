"""Driving the strategy pipeline.

The runner is infrastructure, not strategy logic: it gathers the current state
of every monitored market, hands it to each strategy, and walks each detected
opportunity through pricing, signal generation and validation. Keeping the walk
here rather than in a base class is what stops ``Strategy`` accumulating
behaviour that every future strategy would inherit whether it wanted it or not.

Nothing is executed and nothing is stored. Phase 7 persists what comes out of
here; Phase 8 is the first thing allowed to act on it.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Protocol

from trading_bot.core.logging import get_logger
from trading_bot.exchange.models import FundingInfo, MarketRef, MarketSpec
from trading_bot.marketdata.models import MarketSnapshot
from trading_bot.monitoring.metrics import MarketMetrics
from trading_bot.strategy.base import MarketView, Strategy, StrategyContext
from trading_bot.strategy.models import (
    DetectionStats,
    Edge,
    Opportunity,
    RejectionReason,
    Signal,
    ValidationResult,
)

logger = get_logger(__name__)


class MarketSource(Protocol):
    """The engine-and-monitor read API - all the runner needs from them."""

    def snapshots(self) -> list[MarketSnapshot]: ...

    def metrics(self) -> list[MarketMetrics]: ...


@dataclass(frozen=True, slots=True)
class EvaluatedOpportunity:
    """One opportunity and everything the pipeline concluded about it.

    Both the traded and the rejected end up here. A dataset of only the former
    cannot say how many chances existed or what killed them.
    """

    opportunity: Opportunity
    edge: Edge | None
    signal: Signal | None
    validation: ValidationResult | None
    # Set when the opportunity did not become a valid signal.
    rejection: RejectionReason | None = None
    detail: str | None = None

    @property
    def is_actionable(self) -> bool:
        return self.signal is not None and self.rejection is None

    @property
    def net_edge_bps(self) -> Decimal | None:
        return self.edge.net_edge_bps if self.edge else None


@dataclass(frozen=True, slots=True)
class StrategyEvaluation:
    """What one strategy concluded this cycle."""

    strategy: str
    evaluated_at: datetime
    opportunities: tuple[EvaluatedOpportunity, ...]
    stats: DetectionStats | None = None

    @property
    def actionable(self) -> tuple[EvaluatedOpportunity, ...]:
        return tuple(item for item in self.opportunities if item.is_actionable)

    @property
    def rejections(self) -> Counter[RejectionReason]:
        return Counter(item.rejection for item in self.opportunities if item.rejection is not None)

    def best(self) -> EvaluatedOpportunity | None:
        """Highest net edge seen this cycle, whether or not it was actionable."""
        priced = [(item.edge.net_edge_bps, item) for item in self.opportunities if item.edge]
        if not priced:
            return None
        return max(priced, key=lambda pair: pair[0])[1]


def _utcnow() -> datetime:
    return datetime.now(UTC)


class StrategyRunner:
    """Feeds monitored markets to strategies and collects their conclusions."""

    def __init__(
        self,
        strategies: Sequence[Strategy],
        context: StrategyContext,
        *,
        specs: Mapping[MarketRef, MarketSpec] | None = None,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._strategies = list(strategies)
        self._context = context
        self._specs = dict(specs or {})
        self._clock = clock
        self._funding: dict[MarketRef, FundingInfo] = {}
        self._latest: list[StrategyEvaluation] = []
        for strategy in self._strategies:
            strategy.initialize(context)

    # --- inputs -----------------------------------------------------------

    def set_funding(self, rates: Mapping[MarketRef, FundingInfo]) -> None:
        """Latest funding state, polled on the venue's cadence rather than ours."""
        self._funding = dict(rates)

    # --- the cycle --------------------------------------------------------

    def evaluate(
        self,
        snapshots: Sequence[MarketSnapshot],
        metrics: Sequence[MarketMetrics] = (),
    ) -> list[StrategyEvaluation]:
        """One pass: every strategy sees every market, and prices what it finds."""
        now = self._clock()
        views = self._views(snapshots, metrics)
        self._latest = [self._evaluate_one(strategy, views, now) for strategy in self._strategies]
        return list(self._latest)

    def evaluations(self) -> list[StrategyEvaluation]:
        return list(self._latest)

    async def run(self, source: MarketSource, *, interval_seconds: float) -> None:
        """Evaluate on a fixed cadence, like the monitor it reads from."""
        while True:
            self.evaluate(source.snapshots(), source.metrics())
            await asyncio.sleep(interval_seconds)

    # --- internals --------------------------------------------------------

    def _views(
        self, snapshots: Sequence[MarketSnapshot], metrics: Sequence[MarketMetrics]
    ) -> list[MarketView]:
        by_ref = {metric.ref: metric for metric in metrics}
        return [
            MarketView(
                snapshot=snapshot,
                metrics=by_ref.get(snapshot.ref),
                spec=self._specs.get(snapshot.ref),
                funding=self._funding.get(snapshot.ref),
            )
            for snapshot in snapshots
        ]

    def _evaluate_one(
        self, strategy: Strategy, views: Sequence[MarketView], now: datetime
    ) -> StrategyEvaluation:
        strategy.on_market_data(views, now)
        evaluated = [
            self._walk(strategy, opportunity) for opportunity in strategy.detect_opportunities()
        ]
        stats = strategy.detection_stats()
        return StrategyEvaluation(
            strategy=strategy.name,
            evaluated_at=now,
            opportunities=tuple(evaluated),
            stats=stats,
        )

    def _walk(self, strategy: Strategy, opportunity: Opportunity) -> EvaluatedOpportunity:
        """detect -> price -> generate -> validate, recording where it stopped."""
        edge = strategy.calculate_edge(opportunity)
        if edge is None:
            return EvaluatedOpportunity(
                opportunity=opportunity,
                edge=None,
                signal=None,
                validation=None,
                rejection=RejectionReason.FUNDING_UNKNOWN,
                detail="no funding interval published for the perpetual leg",
            )
        signal = strategy.generate_signal(opportunity, edge)
        if signal is None:
            return EvaluatedOpportunity(
                opportunity=opportunity,
                edge=edge,
                signal=None,
                validation=None,
                rejection=RejectionReason.BELOW_MIN_EDGE,
                detail=f"net {edge.net_edge_bps:.2f} bps",
            )
        validation = strategy.validate_signal(signal)
        if not validation.is_valid:
            return EvaluatedOpportunity(
                opportunity=opportunity,
                edge=edge,
                signal=None,
                validation=validation,
                rejection=validation.reason,
                detail=validation.detail,
            )
        return EvaluatedOpportunity(
            opportunity=opportunity, edge=edge, signal=signal, validation=validation
        )
