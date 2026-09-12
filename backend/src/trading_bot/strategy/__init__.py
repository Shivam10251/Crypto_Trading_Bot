"""Strategy layer: detect discrepancies, price them honestly, emit signals.

Nothing here imports the database, an exchange client, FastAPI or the
dashboard, which is what lets one strategy object run unchanged in backtest,
paper and live modes. Execution is not part of this layer and does not exist
until Phase 8.
"""

from trading_bot.strategy.base import MarketView, Strategy, StrategyContext
from trading_bot.strategy.basis import STRATEGY_NAME as SPOT_PERP_BASIS
from trading_bot.strategy.basis import BasisPair, SpotPerpBasisStrategy
from trading_bot.strategy.costs import (
    COST_MODEL_VERSION,
    CostModel,
    TransactionCostModel,
    settlements_crossed,
)
from trading_bot.strategy.evidence import (
    FundingEvidence,
    LegEvidence,
    MarketEvidence,
    PricingEvidence,
)
from trading_bot.strategy.fees import FeeSchedule, InstrumentFees, OrderRole
from trading_bot.strategy.models import (
    CostBreakdown,
    DetectionStats,
    Edge,
    Leg,
    Opportunity,
    PricingRefusal,
    PricingResult,
    RejectionReason,
    Signal,
    ValidationResult,
)
from trading_bot.strategy.registry import UnknownStrategyError, build_strategies
from trading_bot.strategy.runner import (
    EvaluatedOpportunity,
    StrategyEvaluation,
    StrategyRunner,
)

__all__ = [
    "COST_MODEL_VERSION",
    "SPOT_PERP_BASIS",
    "BasisPair",
    "CostBreakdown",
    "CostModel",
    "DetectionStats",
    "Edge",
    "EvaluatedOpportunity",
    "FeeSchedule",
    "FundingEvidence",
    "InstrumentFees",
    "Leg",
    "LegEvidence",
    "MarketEvidence",
    "MarketView",
    "Opportunity",
    "OrderRole",
    "PricingEvidence",
    "PricingRefusal",
    "PricingResult",
    "RejectionReason",
    "Signal",
    "SpotPerpBasisStrategy",
    "Strategy",
    "StrategyContext",
    "StrategyEvaluation",
    "StrategyRunner",
    "TransactionCostModel",
    "UnknownStrategyError",
    "ValidationResult",
    "build_strategies",
    "settlements_crossed",
]
