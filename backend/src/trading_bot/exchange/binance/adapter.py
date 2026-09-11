"""binance.com adapter - market data only.

Covers spot and USD-M perpetual futures because the first strategy compares the
two. Routing by ``MarketType`` is the adapter's main job: the two are separate
hosts with different payload shapes.

Execution is not implemented. The inherited guards in ``ExchangeAdapter`` raise
``ExecutionNotEnabledError``, so a strategy that tries to place an order fails
immediately and loudly rather than silently doing nothing.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from time import perf_counter
from typing import Any

from trading_bot.core.config import ExchangeConfig
from trading_bot.core.logging import get_logger
from trading_bot.db.models.enums import MarketType
from trading_bot.exchange.base import DEFAULT_DEPTH_LEVELS, DEFAULT_TRADE_LIMIT, ExchangeAdapter
from trading_bot.exchange.binance.endpoints import normalize_depth_limit, routes_for
from trading_bot.exchange.binance.mapping import (
    parse_daily_stats,
    parse_funding,
    parse_funding_intervals,
    parse_market_spec,
    parse_order_book,
    parse_quote,
    parse_trade,
)
from trading_bot.exchange.binance.rest import BinanceRestClient
from trading_bot.exchange.binance.streams import BinanceStreamSource
from trading_bot.exchange.errors import (
    ExchangeDataError,
    ExchangeError,
    NotSupportedError,
    UnknownMarketError,
)
from trading_bot.exchange.models import (
    FundingInfo,
    MarketRef,
    MarketSpec,
    OrderBook,
    Quote,
    ServerTime,
    TickerStats,
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
        # Funding intervals change on the order of weeks; fetched once, reused.
        self._funding_intervals: Mapping[str, int] | None = None

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
        intervals = await self._funding_intervals_best_effort(ref.market_type)
        return parse_funding(
            payload,
            ref,
            local_timestamp=datetime.now(UTC),
            interval_hours=intervals.get(ref.symbol),
        )

    async def get_funding_rates(self, market_type: MarketType) -> list[FundingInfo]:
        """Funding state for every perpetual, in one request.

        ``premiumIndex`` without a symbol costs weight 10 whether it covers one
        market or nine hundred, so a strategy following fifty pairs polls once
        rather than fifty times. Entries that cannot be normalized are skipped
        rather than failing the batch.
        """
        if market_type is MarketType.SPOT:
            raise NotSupportedError("spot markets have no funding rate")
        routes = routes_for(market_type)
        if routes.premium_index is None:  # pragma: no cover - futures always have it
            raise NotSupportedError(f"no funding endpoint for {market_type.value}")
        payload = await self._client.get(routes.url(routes.premium_index))
        if not isinstance(payload, list):
            raise ExchangeDataError(f"premiumIndex for {market_type.value} is not a list")
        intervals = await self._funding_intervals_best_effort(market_type)
        received_at = datetime.now(UTC)
        rates: list[FundingInfo] = []
        skipped = 0
        for entry in payload:
            symbol = entry.get("symbol") if isinstance(entry, dict) else None
            if not isinstance(symbol, str):
                skipped += 1
                continue
            ref = self.market_ref(symbol, market_type)
            try:
                rates.append(
                    parse_funding(
                        entry,
                        ref,
                        local_timestamp=received_at,
                        interval_hours=intervals.get(symbol),
                    )
                )
            except ExchangeDataError:
                skipped += 1
        logger.info(
            "binance.funding_rates_loaded",
            count=len(rates),
            market_type=market_type.value,
            without_interval=sum(1 for rate in rates if rate.funding_interval_hours is None),
            skipped=skipped,
        )
        return rates

    async def _funding_intervals_best_effort(self, market_type: MarketType) -> Mapping[str, int]:
        """Intervals when the venue answers, empty when it does not.

        ``fundingInfo`` is a second endpoint enriching the rate, so its outage
        must not take down a call that would otherwise succeed. A market left
        without an interval is reported as unknown - which the cost model
        refuses to price - rather than silently defaulted.
        """
        try:
            return await self.get_funding_intervals(market_type)
        except ExchangeError as exc:
            logger.warning("binance.funding_intervals_unavailable", error=str(exc))
            return {}

    async def get_funding_intervals(self, market_type: MarketType) -> Mapping[str, int]:
        """How often each perpetual settles funding, in hours.

        Cached for the life of the adapter: the venue changes these on the
        order of weeks, and the rate is meaningless without them. Symbols the
        venue omits stay absent rather than being defaulted - measured live,
        the omitted ones are not all on the eight-hour grid either.
        """
        if market_type is MarketType.SPOT:
            raise NotSupportedError("spot markets have no funding interval")
        cached = self._funding_intervals
        if cached is not None:
            return cached
        routes = routes_for(market_type)
        if routes.funding_info is None:  # pragma: no cover - futures always have it
            raise NotSupportedError(f"no funding interval endpoint for {market_type.value}")
        payload = await self._client.get(routes.url(routes.funding_info))
        intervals = parse_funding_intervals(payload)
        self._funding_intervals = intervals
        logger.info("binance.funding_intervals_loaded", count=len(intervals))
        return intervals

    async def get_daily_stats(self, market_type: MarketType) -> list[TickerStats]:
        """Rolling 24h statistics for every symbol of one instrument class.

        One request for the whole venue (weight 80 spot, 40 futures) instead of
        one per symbol. Entries that cannot be normalized - halted pairs report
        a zero last price - are skipped rather than failing the batch: they
        cannot be monitored anyway.
        """
        routes = routes_for(market_type)
        payload = await self._client.get(routes.url(routes.ticker_24hr))
        if not isinstance(payload, list):
            raise ExchangeDataError(f"ticker/24hr for {market_type.value} is not a list")
        received_at = datetime.now(UTC)
        stats: list[TickerStats] = []
        skipped = 0
        for entry in payload:
            symbol = entry.get("symbol") if isinstance(entry, dict) else None
            if not isinstance(symbol, str):
                skipped += 1
                continue
            try:
                ref = self.market_ref(symbol, market_type)
                stats.append(parse_daily_stats(entry, ref, received_at))
            except ExchangeDataError:
                skipped += 1
        logger.info(
            "binance.daily_stats_loaded",
            market_type=market_type.value,
            count=len(stats),
            skipped=skipped,
            used_weight=self._client.used_weight,
        )
        return stats

    def stream_source(self) -> BinanceStreamSource:
        """Stream routing and parsing for the market-data engine.

        WebSocket hosts come from configuration so a testnet or regional mirror
        can be used without code changes; paths are chosen per stream kind.
        """
        if self._config is None:
            return BinanceStreamSource(venue=self.venue)
        return BinanceStreamSource(
            venue=self.venue,
            spot_ws_base=self._config.spot_ws_url,
            futures_ws_base=self._config.futures_ws_url,
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
