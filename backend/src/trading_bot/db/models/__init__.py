"""ORM models.

Importing this package registers every table on ``Base.metadata``, which is
what Alembic autogenerate and the test schema builder rely on.
"""

from trading_bot.db.models.enums import (
    ExecutionMode,
    MarketType,
    OpportunityStatus,
    OrderStatus,
    OrderType,
    PositionStatus,
    RiskDecision,
    RiskEventType,
    Severity,
    Side,
    SignalStatus,
    SystemEventType,
    TimeInForce,
    ValuationStatus,
)
from trading_bot.db.models.events import RiskEvent, SystemEvent
from trading_bot.db.models.execution import Fill, Order, Position
from trading_bot.db.models.market import Market, MarketData, MarketTrade, OrderBookSnapshot
from trading_bot.db.models.portfolio import PnlSnapshot, PortfolioSnapshot
from trading_bot.db.models.research import Opportunity, Signal

# Grouped by kind rather than alphabetically: the tables read in pipeline
# order, which is more useful here than sorting.
__all__ = [  # noqa: RUF022
    # tables
    "Market",
    "MarketData",
    "OrderBookSnapshot",
    "MarketTrade",
    "Opportunity",
    "Signal",
    "Order",
    "Fill",
    "Position",
    "PortfolioSnapshot",
    "PnlSnapshot",
    "RiskEvent",
    "SystemEvent",
    # enums
    "ExecutionMode",
    "MarketType",
    "OpportunityStatus",
    "OrderStatus",
    "OrderType",
    "PositionStatus",
    "RiskDecision",
    "RiskEventType",
    "Severity",
    "Side",
    "SignalStatus",
    "SystemEventType",
    "TimeInForce",
    "ValuationStatus",
]
