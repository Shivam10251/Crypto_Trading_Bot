"""The strategy contract.

A strategy consumes normalized market data and emits signals. It may import
the domain types in ``trading_bot.strategy.models``, ``trading_bot.exchange.models``
and ``trading_bot.marketdata.models`` - and nothing else from the platform. No
database, no exchange client, no FastAPI, no dashboard. That restriction is
what allows the same object to run in backtest, paper and live modes.

The base class is deliberately thin. Shared maths belongs in helper modules,
not in a base class that accumulates behaviour, so ``Strategy`` declares the
five steps of the pipeline and implements none of them. The pipeline itself -
detect, price, generate, validate - is driven by ``runner.StrategyRunner``,
which is infrastructure rather than strategy logic.

``detect_opportunities`` is plural where ``docs/strategy.md`` first sketched a
singular ``detect_opportunity``: Phase 4 made the platform monitor fifty pairs
at once, and a per-cycle API that can return only one of them would hide the
other forty-nine.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import ClassVar

from trading_bot.exchange.models import FundingInfo, MarketRef, MarketSpec
from trading_bot.marketdata.models import MarketSnapshot
from trading_bot.monitoring.metrics import MarketMetrics
from trading_bot.strategy.costs import CostModel
from trading_bot.strategy.models import (
    DetectionStats,
    Edge,
    Opportunity,
    Signal,
    ValidationResult,
)


@dataclass(frozen=True, slots=True)
class MarketView:
    """Everything a strategy is allowed to know about one market right now."""

    snapshot: MarketSnapshot
    metrics: MarketMetrics | None = None
    spec: MarketSpec | None = None
    funding: FundingInfo | None = None

    @property
    def ref(self) -> MarketRef:
        return self.snapshot.ref


@dataclass(frozen=True, slots=True)
class StrategyContext:
    """What a strategy is given at initialization.

    Handed in rather than imported, so a strategy has no way to reach
    configuration, a clock or a cost model the test did not provide.
    """

    cost_model: CostModel
    # Instrument reference data: tick size, step size and minimum notional -
    # the difference between a trade that can be placed and one that cannot.
    specs: Mapping[MarketRef, MarketSpec] = field(default_factory=dict)

    def spec(self, ref: MarketRef) -> MarketSpec | None:
        return self.specs.get(ref)


class Strategy(ABC):
    """One trading idea. Each lives in its own module; none inherit from another."""

    #: Stable identifier, stored on every opportunity and signal it produces.
    name: ClassVar[str]

    @abstractmethod
    def initialize(self, context: StrategyContext) -> None:
        """Accept the cost model and reference data. Called once, before data."""

    @abstractmethod
    def on_market_data(self, views: Sequence[MarketView], now: datetime) -> None:
        """Take the latest state of every market the strategy follows."""

    @abstractmethod
    def detect_opportunities(self) -> list[Opportunity]:
        """Every discrepancy currently visible, profitable or not.

        Unprofitable ones are returned deliberately: a dataset containing only
        the trades we would have taken cannot say how many chances existed.
        """

    @abstractmethod
    def calculate_edge(self, opportunity: Opportunity) -> Edge | None:
        """Apply the cost model. ``None`` when a cost cannot be estimated."""

    @abstractmethod
    def generate_signal(self, opportunity: Opportunity, edge: Edge) -> Signal | None:
        """Intent to trade, or ``None`` when the surviving edge is too thin."""

    @abstractmethod
    def validate_signal(self, signal: Signal) -> ValidationResult:
        """Last check before a signal leaves the strategy: is it still real?"""

    def detection_stats(self) -> DetectionStats | None:
        """Why the markets seen produced no opportunity; ``None`` if not tracked.

        Not abstract: a strategy is free not to explain its silence, but the
        first one does, because zero opportunities and a broken feed look
        identical from the outside.
        """
        return None
