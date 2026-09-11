"""The exchange boundary.

``ExchangeAdapter`` is the only thing the rest of the platform knows about a
venue. Strategies depend on this interface and on the normalized models, never
on Binance specifics, so supporting a second venue means writing one adapter.

Capability pattern: market-data methods are abstract because every venue must
provide them. Optional capabilities (perpetual funding) and the execution
methods have default implementations that raise, so an adapter only implements
what its venue actually supports - and a missing capability fails loudly instead
of returning something invented.

Execution in particular is *deliberately* unavailable: Phase 2 is market data
only, and the order path is built in Phase 17 behind an explicit opt-in.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from decimal import Decimal
from types import TracebackType
from typing import Self

from trading_bot.db.models.enums import MarketType
from trading_bot.exchange.errors import ExecutionNotEnabledError, NotSupportedError
from trading_bot.exchange.models import (
    Balance,
    FundingInfo,
    MarketRef,
    MarketSpec,
    OrderBook,
    Quote,
    ServerTime,
    TickerStats,
    TradePrint,
)
from trading_bot.exchange.streaming import MarketStreamSource

DEFAULT_DEPTH_LEVELS = 10
DEFAULT_TRADE_LIMIT = 50


class ExchangeAdapter(ABC):
    """A venue, reduced to the operations this platform needs."""

    #: Venue identifier stored on every row this adapter produces.
    venue: str

    # --- market data (every adapter must implement) -----------------------

    @abstractmethod
    async def get_markets(self, market_type: MarketType | None = None) -> list[MarketSpec]:
        """List tradeable instruments, optionally filtered by type."""

    @abstractmethod
    async def get_ticker(self, ref: MarketRef) -> Quote:
        """Current top of book for one market."""

    @abstractmethod
    async def get_order_book(self, ref: MarketRef, levels: int = DEFAULT_DEPTH_LEVELS) -> OrderBook:
        """Depth snapshot for one market."""

    @abstractmethod
    async def get_recent_trades(
        self, ref: MarketRef, limit: int = DEFAULT_TRADE_LIMIT
    ) -> list[TradePrint]:
        """Recent public trades for one market."""

    @abstractmethod
    async def get_server_time(self) -> ServerTime:
        """Venue clock, for measuring skew against ours."""

    @abstractmethod
    def stream_source(self) -> MarketStreamSource:
        """The venue's half of live streaming: stream URLs and message parsing.

        Connection management, reconnection, staleness and order-book
        synchronisation are venue-independent and live in
        ``trading_bot.marketdata``. Strategies consume that engine's normalized
        snapshots, never a WebSocket.
        """

    # --- optional capabilities -------------------------------------------

    async def get_funding(self, ref: MarketRef) -> FundingInfo:
        """Funding state for a perpetual market.

        Default raises: venues without perpetuals should not pretend to answer.
        """
        raise NotSupportedError(f"{self.venue} does not expose funding for {ref}")

    async def get_daily_stats(self, market_type: MarketType) -> list[TickerStats]:
        """Rolling 24h statistics for every listed symbol of one instrument class.

        Used to rank markets by liquidity. Default raises: a venue without a
        bulk endpoint should not quietly make one request per symbol.
        """
        raise NotSupportedError(f"{self.venue} does not expose bulk 24h statistics")

    # --- execution (disabled until Phase 17) ------------------------------

    async def place_order(self, *args: object, **kwargs: object) -> object:
        raise ExecutionNotEnabledError(
            "order placement is not implemented; execution arrives in Phase 17 "
            "and stays disabled until explicitly armed"
        )

    async def cancel_order(self, *args: object, **kwargs: object) -> object:
        raise ExecutionNotEnabledError("order cancellation is not implemented")

    async def get_order_status(self, *args: object, **kwargs: object) -> object:
        raise ExecutionNotEnabledError("order status is not implemented")

    async def get_balances(self) -> Sequence[Balance]:
        raise ExecutionNotEnabledError(
            "balances require authenticated API access, which is not enabled"
        )

    # --- lifecycle --------------------------------------------------------

    async def aclose(self) -> None:
        """Release connections. Safe to call more than once.

        Adapters holding sockets override this; the default is a no-op so a
        stateless adapter need not implement it.
        """
        return None

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.aclose()

    # --- helpers shared by adapters ---------------------------------------

    def market_ref(self, symbol: str, market_type: MarketType) -> MarketRef:
        """Build a ref belonging to this venue, with a normalized symbol."""
        return MarketRef(venue=self.venue, symbol=symbol.upper(), market_type=market_type)

    @staticmethod
    def round_to_tick(price: Decimal, tick_size: Decimal | None) -> Decimal:
        """Snap a price to the venue's tick size.

        Lives here because every venue has the same requirement, and an order
        violating it is rejected - which the paper simulator must reproduce.
        """
        if not tick_size:
            return price
        return (price / tick_size).quantize(Decimal(1)) * tick_size
