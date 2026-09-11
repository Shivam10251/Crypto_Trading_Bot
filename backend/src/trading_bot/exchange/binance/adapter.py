"""binance.com adapter - market data only.

Covers spot and USD-M perpetual futures because the first strategy compares the
two. Routing by ``MarketType`` is the adapter's main job: the two are separate
hosts with different payload shapes.

Execution is not implemented. The inherited guards in ``ExchangeAdapter`` raise
``ExecutionNotEnabledError``, so a strategy that tries to place an order fails
immediately and loudly rather than silently doing nothing.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from time import perf_counter
from typing import Any

from trading_bot.core.config import ExchangeConfig
from trading_bot.core.logging import get_logger
from trading_bot.db.models.enums import MarketType
from trading_bot.exchange.base import DEFAULT_DEPTH_LEVELS, DEFAULT_TRADE_LIMIT, ExchangeAdapter
from trading_bot.exchange.binance.endpoints import normalize_depth_limit, routes_for
from trading_bot.exchange.binance.mapping import (
    parse_funding,
    parse_market_spec,
    parse_order_book,
    parse_quote,
    parse_trade,
)
from trading_bot.exchange.binance.rest import BinanceRestClient
from trading_bot.exchange.errors import (
    ExchangeDataError,
    NotSupportedError,
    UnknownMarketError,
)
from trading_bot.exchange.models import (
    FundingInfo,
    MarketDataSubscription,
    MarketRef,
    MarketSpec,
    OrderBook,
    Quote,
    ServerTime,
    TradePrint,
)

logger = get_logger(__name__)

VENUE = "binance"


class BinanceExchangeAdapter(ExchangeAdapter):
    """Market-data adapter for binance.com spot and USD-M futures."""

    venue = VENUE

    def __init__(
        self,
        config: ExchangeConfig | None = None,
        *,
        client: BinanceRestClient | None = None,
    ) -> None:
        self._config = config
        timeout = config.request_timeout_seconds if config else 10
        self._client = client or BinanceRestClient(timeout_seconds=timeout)
        self._owns_client = client is None

    # --- market data ------------------------------------------------------

    async def get_markets(self, market_type: MarketType | None = None) -> list[MarketSpec]:
        """Instruments from ``exchangeInfo``.

        With no filter, both spot and futures are fetched: the strategy needs
        both sides of the basis, and callers should not have to know that means
        two requests.
        """
        wanted = (
            (market_type,) if market_type is not None else (MarketType.SPOT, MarketType.PERPETUAL)
        )
        specs: list[MarketSpec] = []
        for kind in wanted:
            routes = routes_for(kind)
            payload = await self._client.get(routes.url(routes.exchange_info))
            symbols = payload.get("symbols") if isinstance(payload, dict) else None
            if not isinstance(symbols, list):
                raise ExchangeDataError(f"exchangeInfo for {kind.value} has no symbol list")
            for entry in symbols:
                if not isinstance(entry, dict):
                    continue
                # Futures exchangeInfo lists delivery contracts alongside
                # perpetuals; only perpetuals belong under PERPETUAL.
                if kind is MarketType.PERPETUAL and entry.get("contractType") not in (
                    None,
                    "PERPETUAL",
                ):
                    continue
                specs.append(parse_market_spec(entry, self.venue, kind))

        logger.info(
            "binance.markets_loaded",
            count=len(specs),
            types=[kind.value for kind in wanted],
            used_weight=self._client.used_weight,
        )
        return specs

    async def get_ticker(self, ref: MarketRef) -> Quote:
        routes = routes_for(ref.market_type)
        payload = await self._fetch_symbol(
            routes.url(routes.book_ticker), ref, context="bookTicker"
        )
        return parse_quote(payload, ref, local_timestamp=datetime.now(UTC))

    async def get_order_book(self, ref: MarketRef, levels: int = DEFAULT_DEPTH_LEVELS) -> OrderBook:
        if levels <= 0:
            raise ValueError("levels must be positive")
        routes = routes_for(ref.market_type)
        payload = await self._fetch_symbol(
            routes.url(routes.depth),
            ref,
            context="depth",
            # The venue rejects arbitrary limits, so snap to an allowed one.
            extra={"limit": normalize_depth_limit(levels)},
        )
        book = parse_order_book(payload, ref, local_timestamp=datetime.now(UTC))
        # Honour what the caller asked for even though the venue rounded up.
        if len(book.bids) > levels or len(book.asks) > levels:
            return OrderBook(
                ref=book.ref,
                bids=book.bids[:levels],
                asks=book.asks[:levels],
                local_timestamp=book.local_timestamp,
                exchange_timestamp=book.exchange_timestamp,
                sequence=book.sequence,
            )
        return book

    async def get_recent_trades(
        self, ref: MarketRef, limit: int = DEFAULT_TRADE_LIMIT
    ) -> list[TradePrint]:
        if limit <= 0:
            raise ValueError("limit must be positive")
        routes = routes_for(ref.market_type)
        payload = await self._fetch_symbol(
            routes.url(routes.trades), ref, context="trades", extra={"limit": limit}
        )
        if not isinstance(payload, list):
            raise ExchangeDataError(f"trades for {ref} is not a list")
        received_at = datetime.now(UTC)
        return [parse_trade(entry, ref, received_at) for entry in payload]

    async def get_server_time(self) -> ServerTime:
        """Venue clock and round-trip time, for skew measurement."""
        routes = routes_for(MarketType.SPOT)
        started = perf_counter()
        payload = await self._client.get(routes.url(routes.server_time))
        round_trip_ms = int((perf_counter() - started) * 1000)
        raw = payload.get("serverTime") if isinstance(payload, dict) else None
        if raw is None:
            raise ExchangeDataError("server time response has no serverTime")
        from trading_bot.exchange.binance.mapping import to_datetime

        return ServerTime(
            exchange_time=to_datetime(raw, "serverTime"),
            local_time=datetime.now(UTC),
            round_trip_ms=round_trip_ms,
        )

    async def get_funding(self, ref: MarketRef) -> FundingInfo:
        """Funding state for a perpetual. Spot markets have none."""
        if ref.market_type is MarketType.SPOT:
            raise NotSupportedError(f"spot market {ref.symbol} has no funding rate")
        routes = routes_for(ref.market_type)
        if routes.premium_index is None:  # pragma: no cover - futures always have it
            raise NotSupportedError(f"no funding endpoint for {ref}")
        payload = await self._fetch_symbol(
            routes.url(routes.premium_index), ref, context="premiumIndex"
        )
        return parse_funding(payload, ref, local_timestamp=datetime.now(UTC))

    def subscribe_market_data(self, subscription: MarketDataSubscription) -> AsyncIterator[Quote]:
        """Not implemented in Phase 2.

        The streaming engine - connection management, reconnection, heartbeats,
        staleness detection and order-book synchronisation - is Phase 3. Raising
        here is better than a half-built stream that silently stops delivering.
        """
        raise NotSupportedError(
            "live market-data streaming is implemented in Phase 3; "
            "use get_ticker/get_order_book for snapshots"
        )

    # --- internals --------------------------------------------------------

    async def _fetch_symbol(
        self,
        url: str,
        ref: MarketRef,
        *,
        context: str,
        extra: dict[str, Any] | None = None,
    ) -> Any:
        """GET a symbol-scoped endpoint, mapping "unknown symbol" to our error."""
        params: dict[str, Any] = {"symbol": ref.symbol, **(extra or {})}
        from trading_bot.exchange.errors import ExchangeResponseError

        try:
            return await self._client.get(url, params=params)
        except ExchangeResponseError as exc:
            # -1121 invalid symbol (spot), -1122 (futures variants).
            if exc.code in (-1121, -1122) or (exc.status_code == 400 and "symbol" in str(exc)):
                raise UnknownMarketError(
                    f"{ref.symbol} is not listed on binance {ref.market_type.value}"
                ) from exc
            logger.warning("binance.request_failed", context=context, ref=str(ref), error=str(exc))
            raise

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()
