"""Exchange abstraction.

The platform talks to venues only through ``ExchangeAdapter`` and the
normalized models here, so a second exchange means a new adapter rather than
changes to strategy, risk or execution code.
"""

from trading_bot.exchange.base import ExchangeAdapter
from trading_bot.exchange.errors import (
    ExchangeConnectionError,
    ExchangeDataError,
    ExchangeError,
    ExchangeRateLimitError,
    ExchangeResponseError,
    ExecutionNotEnabledError,
    NotSupportedError,
    UnknownMarketError,
)
from trading_bot.exchange.models import (
    Balance,
    BookLevel,
    FundingInfo,
    MarketDataSubscription,
    MarketRef,
    MarketSpec,
    OrderBook,
    Quote,
    ServerTime,
    TradePrint,
)

__all__ = [  # noqa: RUF022 - grouped by kind, which reads better here
    # boundary
    "ExchangeAdapter",
    # models
    "MarketRef",
    "MarketSpec",
    "Quote",
    "OrderBook",
    "BookLevel",
    "TradePrint",
    "FundingInfo",
    "Balance",
    "ServerTime",
    "MarketDataSubscription",
    # errors
    "ExchangeError",
    "ExchangeConnectionError",
    "ExchangeRateLimitError",
    "ExchangeResponseError",
    "ExchangeDataError",
    "UnknownMarketError",
    "ExecutionNotEnabledError",
    "NotSupportedError",
]
