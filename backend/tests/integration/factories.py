"""Row builders for data-model tests.

Explicit helpers rather than a factory library: the tests are about what the
database accepts, so the values that matter should be visible at the call site.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from trading_bot.db.models import (
    ExecutionMode,
    Market,
    MarketData,
    MarketType,
    Opportunity,
    OpportunityStatus,
    Order,
    OrderStatus,
    OrderType,
    Side,
    Signal,
    SignalStatus,
)

NOW = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)


def make_market(
    symbol: str = "BTCUSDT",
    market_type: MarketType = MarketType.SPOT,
    **overrides: object,
) -> Market:
    defaults: dict[str, object] = {
        "venue": "binance",
        "symbol": symbol,
        "market_type": market_type,
        "base_asset": "BTC",
        "quote_asset": "USDT",
        "tick_size": Decimal("0.01"),
        "step_size": Decimal("0.00001"),
        "min_notional": Decimal("5"),
        "taker_fee_bps": Decimal("10"),
        "is_active": True,
    }
    return Market(**{**defaults, **overrides})  # type: ignore[arg-type]


def make_market_data(
    market: Market,
    bid: str = "100000.00",
    ask: str = "100001.00",
    *,
    at: datetime = NOW,
    **overrides: object,
) -> MarketData:
    bid_d, ask_d = Decimal(bid), Decimal(ask)
    mid = (bid_d + ask_d) / 2
    spread = ask_d - bid_d
    defaults: dict[str, object] = {
        "market": market,
        "bid": bid_d,
        "ask": ask_d,
        "bid_size": Decimal("1.5"),
        "ask_size": Decimal("2.0"),
        "mid_price": mid,
        "spread": spread,
        "spread_bps": (spread / mid) * 10_000,
        "exchange_timestamp": at,
        "local_timestamp": at + timedelta(milliseconds=18),
        "latency_ms": 18,
    }
    return MarketData(**{**defaults, **overrides})  # type: ignore[arg-type]


def make_opportunity(
    market: Market,
    secondary_market: Market | None = None,
    *,
    # None for an UNPRICEABLE row: no cost was ever estimated for it.
    net_edge_bps: str | None = "3.5",
    status: OpportunityStatus = OpportunityStatus.DETECTED,
    **overrides: object,
) -> Opportunity:
    defaults: dict[str, object] = {
        "detected_at": NOW,
        "strategy": "spot_perp_basis",
        "mode": ExecutionMode.THEORETICAL,
        "market": market,
        "secondary_market": secondary_market,
        "direction": Side.BUY,
        "entry_price": Decimal("100000"),
        "exit_price": Decimal("100060"),
        "quantity": Decimal("0.01"),
        "notional_usd": Decimal("1000"),
        "gross_edge_bps": Decimal("6.0"),
        "gross_edge_usd": Decimal("0.60"),
        "estimated_fees_usd": Decimal("0.15"),
        "estimated_slippage_usd": Decimal("0.05"),
        "safety_buffer_usd": Decimal("0.05"),
        "net_edge_bps": Decimal(net_edge_bps) if net_edge_bps is not None else None,
        "net_edge_usd": Decimal("0.35"),
        "liquidity_usd": Decimal("25000"),
        "latency_ms": 18,
        "duration_ms": 240,
        "status": status,
    }
    return Opportunity(**{**defaults, **overrides})  # type: ignore[arg-type]


def make_signal(opportunity: Opportunity, market: Market, **overrides: object) -> Signal:
    defaults: dict[str, object] = {
        "opportunity": opportunity,
        "generated_at": NOW,
        "strategy": "spot_perp_basis",
        "market": market,
        "side": Side.BUY,
        "quantity": Decimal("0.01"),
        "target_entry_price": Decimal("100000"),
        "target_exit_price": Decimal("100060"),
        "expected_net_edge_bps": Decimal("3.5"),
        "status": SignalStatus.GENERATED,
    }
    return Signal(**{**defaults, **overrides})  # type: ignore[arg-type]


def make_order(market: Market, **overrides: object) -> Order:
    defaults: dict[str, object] = {
        "market": market,
        "mode": ExecutionMode.PAPER,
        "client_order_id": "test-order-1",
        "side": Side.BUY,
        "order_type": OrderType.MARKET,
        "quantity": Decimal("0.01"),
        "status": OrderStatus.PENDING,
        "submitted_at": NOW,
    }
    return Order(**{**defaults, **overrides})  # type: ignore[arg-type]
