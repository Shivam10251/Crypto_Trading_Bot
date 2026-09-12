"""Binance payload -> normalized model.

This is the validation boundary. Everything past this module is trusted, so
every field is checked here: missing keys, unparseable numbers and
economically impossible values all raise ``ExchangeDataError`` rather than
propagating a surprise into a strategy.

Verified payload shapes (live API):
  spot bookTicker    {symbol,bidPrice,bidQty,askPrice,askQty}         - no clock
  futures bookTicker {...,time,lastUpdateId}                          - has clock
  spot depth         {lastUpdateId,bids,asks}                         - no clock
  futures depth      {lastUpdateId,E,T,bids,asks}                     - has clock
  trades (both)      [{id,price,qty,time,isBuyerMaker}]               - has clock
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from trading_bot.db.models.enums import MarketType, Side
from trading_bot.exchange.errors import ExchangeDataError
from trading_bot.exchange.models import (
    BookLevel,
    FundingInfo,
    MarketRef,
    MarketSpec,
    OrderBook,
    Quote,
    TickerStats,
    TradePrint,
)

# Filter names and *keys* in exchangeInfo, which differ between spot and
# futures. Verified against the live API on 2026-09-12:
#   spot     NOTIONAL     {"minNotional": "5.00000000", "maxNotional": ...}
#   futures  MIN_NOTIONAL {"notional": "50"}
# All 3,698 spot symbols use the first shape and all 897 USD-M perpetuals the
# second; not one futures symbol publishes a "minNotional" key. Reading only
# "minNotional" left every perpetual's minimum unknown.
_TICK_FILTERS = ("PRICE_FILTER",)
_LOT_FILTER = "LOT_SIZE"
_MARKET_LOT_FILTER = "MARKET_LOT_SIZE"
_NOTIONAL_FILTERS = ("NOTIONAL", "MIN_NOTIONAL")
_NOTIONAL_KEYS = ("minNotional", "notional")


def _filter_decimal(
    raw_filter: dict[str, Any], keys: tuple[str, ...], symbol: str
) -> Decimal | None:
    """First of ``keys`` the venue actually published, as a Decimal."""
    for key in keys:
        if raw_filter.get(key) is not None:
            return to_decimal(raw_filter[key], f"{symbol} {key}")
    return None


def _positive_or_none(value: Decimal | None) -> Decimal | None:
    """Binance publishes ``0`` for a lot filter that constrains nothing."""
    return value if value is not None and value > 0 else None


def _require(payload: Any, key: str, context: str) -> Any:
    if not isinstance(payload, dict) or key not in payload:
        raise ExchangeDataError(f"{context}: missing field {key!r}")
    return payload[key]


def to_decimal(raw: Any, context: str) -> Decimal:
    """Parse a venue-supplied numeric string.

    Binance sends numbers as strings precisely so clients do not lose precision
    to float parsing; going straight to Decimal preserves that.
    """
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ExchangeDataError(f"{context}: cannot parse {raw!r} as a number") from exc
    # "NaN" and "Infinity" parse, but comparing them raises later - far from
    # the payload that caused it.
    if not value.is_finite():
        raise ExchangeDataError(f"{context}: {raw!r} is not a finite number")
    return value


def to_datetime(raw: Any, context: str) -> datetime:
    """Convert Binance milliseconds-since-epoch to an aware UTC datetime."""
    try:
        return datetime.fromtimestamp(int(raw) / 1000, tz=UTC)
    except (TypeError, ValueError, OSError, OverflowError) as exc:
        raise ExchangeDataError(f"{context}: cannot parse {raw!r} as a timestamp") from exc


def parse_market_spec(payload: dict[str, Any], venue: str, market_type: MarketType) -> MarketSpec:
    """One entry from ``exchangeInfo.symbols``.

    Fees are left ``None``: Binance exposes account-specific fees only behind an
    authenticated endpoint, so the cost model uses configured values instead of
    inventing them here.
    """
    context = "exchangeInfo symbol"
    symbol = str(_require(payload, "symbol", context))
    status = str(payload.get("status", "")).upper()

    tick_size: Decimal | None = None
    min_price: Decimal | None = None
    max_price: Decimal | None = None
    percent_price_up: Decimal | None = None
    percent_price_down: Decimal | None = None
    bid_percent_price_up: Decimal | None = None
    bid_percent_price_down: Decimal | None = None
    ask_percent_price_up: Decimal | None = None
    ask_percent_price_down: Decimal | None = None
    percent_price_avg_mins = 0
    min_notional: Decimal | None = None
    max_notional: Decimal | None = None
    min_notional_apply_to_market = True
    max_notional_apply_to_market = False
    notional_avg_price_mins = 0
    lot: dict[str, Any] = {}
    market_lot: dict[str, Any] = {}
    for raw_filter in payload.get("filters", []):
        if not isinstance(raw_filter, dict):
            continue
        kind = raw_filter.get("filterType")
        if kind in _TICK_FILTERS and "tickSize" in raw_filter:
            tick_size = to_decimal(raw_filter["tickSize"], f"{symbol} tickSize")
            min_price = _positive_or_none(_filter_decimal(raw_filter, ("minPrice",), symbol))
            max_price = _positive_or_none(_filter_decimal(raw_filter, ("maxPrice",), symbol))
        elif kind == _LOT_FILTER:
            lot = raw_filter
        elif kind == _MARKET_LOT_FILTER:
            market_lot = raw_filter
        elif kind in _NOTIONAL_FILTERS:
            min_notional = _filter_decimal(raw_filter, _NOTIONAL_KEYS, symbol)
            max_notional = _positive_or_none(_filter_decimal(raw_filter, ("maxNotional",), symbol))
            if kind == "NOTIONAL":
                min_notional_apply_to_market = bool(raw_filter.get("applyMinToMarket", False))
                max_notional_apply_to_market = bool(raw_filter.get("applyMaxToMarket", False))
            else:
                min_notional_apply_to_market = bool(raw_filter.get("applyToMarket", True))
            try:
                notional_avg_price_mins = int(raw_filter.get("avgPriceMins", 0))
            except (TypeError, ValueError) as exc:
                raise ExchangeDataError(
                    f"{symbol} avgPriceMins: {raw_filter.get('avgPriceMins')!r} is not an integer"
                ) from exc
        elif kind in {"PERCENT_PRICE", "PERCENT_PRICE_BY_SIDE"}:
            percent_price_up = _positive_or_none(
                _filter_decimal(raw_filter, ("multiplierUp",), symbol)
            )
            percent_price_down = _positive_or_none(
                _filter_decimal(raw_filter, ("multiplierDown",), symbol)
            )
            bid_percent_price_up = _positive_or_none(
                _filter_decimal(raw_filter, ("bidMultiplierUp",), symbol)
            )
            bid_percent_price_down = _positive_or_none(
                _filter_decimal(raw_filter, ("bidMultiplierDown",), symbol)
            )
            ask_percent_price_up = _positive_or_none(
                _filter_decimal(raw_filter, ("askMultiplierUp",), symbol)
            )
            ask_percent_price_down = _positive_or_none(
                _filter_decimal(raw_filter, ("askMultiplierDown",), symbol)
            )
            try:
                percent_price_avg_mins = int(raw_filter.get("avgPriceMins", 0))
            except (TypeError, ValueError) as exc:
                raise ExchangeDataError(
                    f"{symbol} avgPriceMins: {raw_filter.get('avgPriceMins')!r} is not an integer"
                ) from exc

    return MarketSpec(
        ref=MarketRef(venue=venue, symbol=symbol, market_type=market_type),
        base_asset=str(_require(payload, "baseAsset", context)),
        quote_asset=str(_require(payload, "quoteAsset", context)),
        # Futures use "TRADING" too; anything else means not tradeable now.
        is_active=status == "TRADING",
        tick_size=tick_size,
        min_price=min_price,
        max_price=max_price,
        percent_price_up=percent_price_up,
        percent_price_down=percent_price_down,
        bid_percent_price_up=bid_percent_price_up,
        bid_percent_price_down=bid_percent_price_down,
        ask_percent_price_up=ask_percent_price_up,
        ask_percent_price_down=ask_percent_price_down,
        percent_price_avg_mins=percent_price_avg_mins,
        step_size=_positive_or_none(_filter_decimal(lot, ("stepSize",), symbol)),
        min_notional=min_notional,
        max_notional=max_notional,
        min_notional_apply_to_market=min_notional_apply_to_market,
        max_notional_apply_to_market=max_notional_apply_to_market,
        notional_avg_price_mins=notional_avg_price_mins,
        market_notional_uses_mark_price=market_type is MarketType.PERPETUAL,
        min_qty=_positive_or_none(_filter_decimal(lot, ("minQty",), symbol)),
        max_qty=_positive_or_none(_filter_decimal(lot, ("maxQty",), symbol)),
        market_min_qty=_positive_or_none(_filter_decimal(market_lot, ("minQty",), symbol)),
        market_max_qty=_positive_or_none(_filter_decimal(market_lot, ("maxQty",), symbol)),
        market_step_size=_positive_or_none(_filter_decimal(market_lot, ("stepSize",), symbol)),
        contract_size=(
            to_decimal(payload["contractSize"], f"{symbol} contractSize")
            if payload.get("contractSize") is not None
            else None
        ),
        settlement_asset=(str(payload["marginAsset"]) if payload.get("marginAsset") else None),
    )


def parse_quote(payload: dict[str, Any], ref: MarketRef, local_timestamp: datetime) -> Quote:
    """``bookTicker`` for spot or futures.

    ``time`` is present only on futures; spot quotes therefore carry no
    exchange timestamp and no latency measurement. Substituting local time
    would manufacture a number that looks like a measurement.
    """
    context = f"bookTicker {ref}"
    exchange_timestamp = (
        to_datetime(payload["time"], context) if payload.get("time") is not None else None
    )
    sequence = payload.get("lastUpdateId")
    return Quote(
        ref=ref,
        bid=to_decimal(_require(payload, "bidPrice", context), f"{context} bidPrice"),
        ask=to_decimal(_require(payload, "askPrice", context), f"{context} askPrice"),
        bid_size=to_decimal(_require(payload, "bidQty", context), f"{context} bidQty"),
        ask_size=to_decimal(_require(payload, "askQty", context), f"{context} askQty"),
        local_timestamp=local_timestamp,
        exchange_timestamp=exchange_timestamp,
        sequence=int(sequence) if sequence is not None else None,
    )


def _parse_levels(raw: Any, context: str) -> tuple[BookLevel, ...]:
    if not isinstance(raw, list):
        raise ExchangeDataError(f"{context}: expected a list of levels")
    levels: list[BookLevel] = []
    for entry in raw:
        if not isinstance(entry, (list, tuple)) or len(entry) < 2:
            raise ExchangeDataError(f"{context}: malformed level {entry!r}")
        price = to_decimal(entry[0], f"{context} price")
        size = to_decimal(entry[1], f"{context} size")
        # Binance pads the book with zero-size levels; they are not liquidity.
        if size <= 0:
            continue
        if price <= 0:
            raise ExchangeDataError(f"{context}: non-positive price {price}")
        levels.append(BookLevel(price=price, size=size))
    if not levels:
        raise ExchangeDataError(f"{context}: no levels with size")
    return tuple(levels)


def parse_order_book(
    payload: dict[str, Any], ref: MarketRef, local_timestamp: datetime
) -> OrderBook:
    """``depth`` for spot or futures. ``E`` is futures-only."""
    context = f"depth {ref}"
    exchange_timestamp = (
        to_datetime(payload["E"], context) if payload.get("E") is not None else None
    )
    sequence = payload.get("lastUpdateId")
    return OrderBook(
        ref=ref,
        bids=_parse_levels(_require(payload, "bids", context), f"{context} bids"),
        asks=_parse_levels(_require(payload, "asks", context), f"{context} asks"),
        local_timestamp=local_timestamp,
        exchange_timestamp=exchange_timestamp,
        sequence=int(sequence) if sequence is not None else None,
        bids_complete=False,
        asks_complete=False,
    )


def parse_trade(payload: dict[str, Any], ref: MarketRef, local_timestamp: datetime) -> TradePrint:
    """One entry from ``trades``.

    ``isBuyerMaker`` is inverted to the aggressor: if the buyer was the maker,
    the seller crossed the spread.
    """
    context = f"trade {ref}"
    is_buyer_maker = payload.get("isBuyerMaker")
    aggressor: Side | None = None
    if isinstance(is_buyer_maker, bool):
        aggressor = Side.SELL if is_buyer_maker else Side.BUY
    return TradePrint(
        ref=ref,
        trade_id=str(_require(payload, "id", context)),
        price=to_decimal(_require(payload, "price", context), f"{context} price"),
        quantity=to_decimal(_require(payload, "qty", context), f"{context} qty"),
        exchange_timestamp=to_datetime(_require(payload, "time", context), context),
        local_timestamp=local_timestamp,
        aggressor_side=aggressor,
    )


def parse_funding(
    payload: dict[str, Any],
    ref: MarketRef,
    local_timestamp: datetime,
    interval_hours: int | None = None,
) -> FundingInfo:
    """``premiumIndex`` - mark price, index price and the funding rate.

    ``premiumIndex`` does not say how often the rate settles; that comes from
    ``fundingInfo`` and is passed in. Left ``None``, the consumer knows the
    rate's period is unknown instead of assuming the historical eight hours.
    """
    context = f"premiumIndex {ref}"
    return FundingInfo(
        ref=ref,
        mark_price=to_decimal(_require(payload, "markPrice", context), f"{context} markPrice"),
        index_price=to_decimal(_require(payload, "indexPrice", context), f"{context} indexPrice"),
        last_funding_rate=to_decimal(
            _require(payload, "lastFundingRate", context), f"{context} lastFundingRate"
        ),
        next_funding_time=to_datetime(_require(payload, "nextFundingTime", context), context),
        local_timestamp=local_timestamp,
        funding_interval_hours=interval_hours,
    )


def parse_funding_intervals(payload: object) -> dict[str, int]:
    """``fundingInfo`` - settlement interval per symbol, in hours.

    Symbols the venue omits are simply absent from the result; they are not
    defaulted to eight hours, because measured against the live venue their
    intervals are mixed (most are on the four-hour grid).
    """
    if not isinstance(payload, list):
        raise ExchangeDataError("fundingInfo response is not a list")
    intervals: dict[str, int] = {}
    for entry in payload:
        if not isinstance(entry, dict):
            continue
        symbol = entry.get("symbol")
        hours = entry.get("fundingIntervalHours")
        if isinstance(symbol, str) and isinstance(hours, int) and hours > 0:
            intervals[symbol] = hours
    return intervals


def parse_daily_stats(
    payload: dict[str, Any], ref: MarketRef, local_timestamp: datetime
) -> TickerStats:
    """One entry of the REST ``ticker/24hr`` list (spot and futures alike).

    ``closeTime`` ends the rolling window and serves as the exchange clock.
    """
    context = f"ticker/24hr {ref}"
    return TickerStats(
        ref=ref,
        last_price=to_decimal(_require(payload, "lastPrice", context), f"{context} lastPrice"),
        volume=to_decimal(_require(payload, "volume", context), f"{context} volume"),
        quote_volume=to_decimal(
            _require(payload, "quoteVolume", context), f"{context} quoteVolume"
        ),
        exchange_timestamp=to_datetime(_require(payload, "closeTime", context), context),
        local_timestamp=local_timestamp,
    )
