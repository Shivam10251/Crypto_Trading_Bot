"""The spot/perpetual basis strategy: detection, sizing, and refusal to guess."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from trading_bot.core.config import CostsConfig, SpotPerpBasisConfig
from trading_bot.db.models.enums import MarketType, Side
from trading_bot.exchange.models import (
    BookLevel,
    FundingInfo,
    MarketRef,
    MarketSpec,
    OrderBook,
    Quote,
)
from trading_bot.marketdata.models import BookLiquidity, BookStatus, FeedStatus, MarketSnapshot
from trading_bot.strategy.base import MarketView, StrategyContext
from trading_bot.strategy.basis import SpotPerpBasisStrategy
from trading_bot.strategy.costs import ConfiguredCostModel
from trading_bot.strategy.models import RejectionReason

NOW = datetime(2026, 9, 11, 16, 30, tzinfo=UTC)
SPOT = MarketRef("binance", "BTCUSDT", MarketType.SPOT)
PERP = MarketRef("binance", "BTCUSDT", MarketType.PERPETUAL)
CONFIG = SpotPerpBasisConfig(max_notional_usd=1000.0, min_net_edge_bps=1.0)
# Zero fees isolate the strategy's own logic from the cost model's.
FREE = CostsConfig(spot_taker_fee_bps=0.0, perp_taker_fee_bps=0.0, safety_buffer_bps=0.0)


def book(ref: MarketRef, bid: str, ask: str, size: str = "100") -> OrderBook:
    """A book deep enough that sizing is never the binding constraint."""
    return OrderBook(
        ref=ref,
        bids=(BookLevel(Decimal(bid), Decimal(size)),),
        asks=(BookLevel(Decimal(ask), Decimal(size)),),
        local_timestamp=NOW,
    )


def liquidity(value: str = "500000") -> BookLiquidity:
    return BookLiquidity(
        band_bps=Decimal(10),
        bid_notional=Decimal(value),
        ask_notional=Decimal(value),
        bid_complete=True,
        ask_complete=True,
        reference_notional=Decimal(10_000),
        buy_slippage_bps=Decimal(0),
        sell_slippage_bps=Decimal(0),
    )


def view(
    ref: MarketRef,
    bid: str,
    ask: str,
    *,
    status: FeedStatus = FeedStatus.LIVE,
    book_status: BookStatus = BookStatus.SYNCED,
    age_ms: int = 10,
    latency_ms: int | None = 20,
    size: str = "100",
    funding: FundingInfo | None = None,
    spec: MarketSpec | None = None,
) -> MarketView:
    depth = book(ref, bid, ask, size) if book_status is BookStatus.SYNCED else None
    snapshot = MarketSnapshot(
        ref=ref,
        status=status,
        quote=Quote(
            ref=ref,
            bid=Decimal(bid),
            ask=Decimal(ask),
            bid_size=Decimal(size),
            ask_size=Decimal(size),
            local_timestamp=NOW,
        ),
        book=depth,
        book_status=book_status,
        last_price=None,
        volume_24h=None,
        quote_volume_24h=None,
        latency_ms=latency_ms,
        last_update_at=NOW,
        age_ms=age_ms,
        updates=1,
        gaps=0,
        resyncs=0,
        liquidity=liquidity(),
    )
    return MarketView(snapshot=snapshot, spec=spec, funding=funding)


def funding_info(rate: str = "0", interval: int | None = 8) -> FundingInfo:
    return FundingInfo(
        ref=PERP,
        mark_price=Decimal(100),
        index_price=Decimal(100),
        last_funding_rate=Decimal(rate),
        next_funding_time=NOW + timedelta(hours=4),
        local_timestamp=NOW,
        funding_interval_hours=interval,
    )


def strategy(
    config: SpotPerpBasisConfig = CONFIG,
    costs: CostsConfig = FREE,
    specs: dict[MarketRef, MarketSpec] | None = None,
) -> SpotPerpBasisStrategy:
    instance = SpotPerpBasisStrategy(config)
    instance.initialize(
        StrategyContext(
            cost_model=ConfiguredCostModel(costs, funding_horizon=timedelta(hours=1)),
            specs=specs or {},
        )
    )
    return instance


# --- detection ------------------------------------------------------------


def test_a_rich_perpetual_is_sold_and_spot_is_bought() -> None:
    engine = strategy()
    engine.on_market_data(
        [view(SPOT, "99.99", "100.01"), view(PERP, "100.99", "101.01", funding=funding_info())],
        NOW,
    )
    (opportunity,) = engine.detect_opportunities()
    assert opportunity.buy.ref is SPOT
    assert opportunity.sell.ref is PERP
    assert opportunity.buy.side is Side.BUY
    assert opportunity.gross_edge_bps == Decimal(100)  # 1.00 on a 100 mid


def test_a_cheap_perpetual_is_bought_and_spot_is_sold() -> None:
    engine = strategy()
    engine.on_market_data(
        [view(SPOT, "100.99", "101.01"), view(PERP, "99.99", "100.01", funding=funding_info())],
        NOW,
    )
    (opportunity,) = engine.detect_opportunities()
    assert opportunity.buy.ref is PERP
    assert opportunity.sell.ref is SPOT


def test_identical_mids_produce_no_opportunity() -> None:
    engine = strategy()
    engine.on_market_data(
        [view(SPOT, "99.99", "100.01"), view(PERP, "99.99", "100.01", funding=funding_info())],
        NOW,
    )
    assert engine.detect_opportunities() == []
    assert engine.detection_stats().no_basis == 1


def test_gross_edge_is_measured_mid_to_mid_and_slippage_separately() -> None:
    """Netting the spread into the basis would hide what the spread costs."""
    engine = strategy()
    # Wide books: the mid basis is 100 bps, but each leg fills away from its mid.
    engine.on_market_data(
        [view(SPOT, "99.50", "100.50"), view(PERP, "100.50", "101.50", funding=funding_info())],
        NOW,
    )
    (opportunity,) = engine.detect_opportunities()
    assert opportunity.gross_edge_bps == Decimal(100)
    # Buying spot at 100.50 against a 100.00 mid is half a point of slippage.
    assert opportunity.buy.slippage == Decimal("0.5")
    assert opportunity.sell.slippage == Decimal("0.5")


# --- the legs must be real at the same instant ----------------------------


def test_a_stale_leg_kills_the_pair() -> None:
    """A basis from a live quote and a stale one is an error, not free money."""
    engine = strategy()
    engine.on_market_data(
        [
            view(SPOT, "99.99", "100.01"),
            view(PERP, "100.99", "101.01", age_ms=5000, funding=funding_info()),
        ],
        NOW,
    )
    assert engine.detect_opportunities() == []
    assert engine.detection_stats().unusable[RejectionReason.STALE_DATA] == 1


def test_an_unsynchronised_book_kills_the_pair() -> None:
    engine = strategy()
    engine.on_market_data(
        [
            view(SPOT, "99.99", "100.01", book_status=BookStatus.SYNCING),
            view(PERP, "100.99", "101.01", funding=funding_info()),
        ],
        NOW,
    )
    assert engine.detect_opportunities() == []
    assert engine.detection_stats().unusable[RejectionReason.BOOK_NOT_SYNCED] == 1


def test_a_market_that_is_not_live_kills_the_pair() -> None:
    engine = strategy()
    engine.on_market_data(
        [
            view(SPOT, "99.99", "100.01", status=FeedStatus.STALE),
            view(PERP, "100.99", "101.01", funding=funding_info()),
        ],
        NOW,
    )
    assert engine.detect_opportunities() == []
    assert engine.detection_stats().unusable[RejectionReason.NOT_LIVE] == 1


def test_a_symbol_listed_on_only_one_side_is_not_a_pair() -> None:
    engine = strategy()
    engine.on_market_data([view(SPOT, "99.99", "100.01")], NOW)
    assert engine.detect_opportunities() == []
    assert engine.detection_stats().pairs_seen == 0


# --- sizing ---------------------------------------------------------------


def test_size_is_capped_by_configuration() -> None:
    engine = strategy()
    engine.on_market_data(
        [view(SPOT, "99.99", "100.01"), view(PERP, "100.99", "101.01", funding=funding_info())],
        NOW,
    )
    (opportunity,) = engine.detect_opportunities()
    assert opportunity.notional_usd == Decimal(1000)
    assert opportunity.quantity == Decimal(10)  # 1000 / 100


def test_size_is_capped_by_the_thinner_books_depth() -> None:
    """The trade is bounded by the leg that cannot fill it."""
    engine = strategy()
    engine.on_market_data(
        [
            view(SPOT, "99.99", "100.01", size="4"),
            view(PERP, "100.99", "101.01", size="100", funding=funding_info()),
        ],
        NOW,
    )
    (opportunity,) = engine.detect_opportunities()
    assert opportunity.quantity == Decimal(4)  # not the 10 configuration asked for
    assert opportunity.notional_usd == Decimal(400)


def test_latency_is_the_worse_of_the_two_legs() -> None:
    engine = strategy()
    engine.on_market_data(
        [
            view(SPOT, "99.99", "100.01", latency_ms=30),
            view(PERP, "100.99", "101.01", latency_ms=210, funding=funding_info()),
        ],
        NOW,
    )
    (opportunity,) = engine.detect_opportunities()
    assert opportunity.latency_ms == 210


# --- persistence ----------------------------------------------------------


def test_duration_tracks_how_long_a_direction_has_held() -> None:
    """The open question from the phase: does a basis outlive our latency?"""
    engine = strategy()
    views = [view(SPOT, "99.99", "100.01"), view(PERP, "100.99", "101.01", funding=funding_info())]
    engine.on_market_data(views, NOW)
    assert engine.detect_opportunities()[0].duration_ms == 0
    engine.on_market_data(views, NOW + timedelta(seconds=3))
    assert engine.detect_opportunities()[0].duration_ms == 3000


def test_duration_resets_when_the_basis_flips_sign() -> None:
    engine = strategy()
    rich = [view(SPOT, "99.99", "100.01"), view(PERP, "100.99", "101.01", funding=funding_info())]
    cheap = [view(SPOT, "100.99", "101.01"), view(PERP, "99.99", "100.01", funding=funding_info())]
    engine.on_market_data(rich, NOW)
    engine.detect_opportunities()
    engine.on_market_data(cheap, NOW + timedelta(seconds=3))
    assert engine.detect_opportunities()[0].duration_ms == 0


# --- signals and validation -----------------------------------------------


def test_a_wide_basis_becomes_a_signal() -> None:
    engine = strategy()
    engine.on_market_data(
        [view(SPOT, "99.99", "100.01"), view(PERP, "100.99", "101.01", funding=funding_info())],
        NOW,
    )
    (opportunity,) = engine.detect_opportunities()
    edge = engine.calculate_edge(opportunity)
    assert edge is not None
    signal = engine.generate_signal(opportunity, edge)
    assert signal is not None
    assert engine.validate_signal(signal).is_valid


def test_a_thin_basis_generates_no_signal() -> None:
    """Below the configured net floor, nothing leaves the strategy."""
    engine = strategy()
    engine.on_market_data(
        [view(SPOT, "99.999", "100.001"), view(PERP, "100.0", "100.002", funding=funding_info())],
        NOW,
    )
    (opportunity,) = engine.detect_opportunities()
    edge = engine.calculate_edge(opportunity)
    assert edge is not None
    assert engine.generate_signal(opportunity, edge) is None


def test_real_fees_turn_a_typical_basis_into_no_signal() -> None:
    """With Binance taker fees, a few bps of basis is not a trade."""
    engine = strategy(costs=CostsConfig())  # 10 / 5 / 2 bps
    engine.on_market_data(
        [view(SPOT, "99.999", "100.001"), view(PERP, "100.049", "100.051", funding=funding_info())],
        NOW,
    )
    (opportunity,) = engine.detect_opportunities()
    edge = engine.calculate_edge(opportunity)
    assert edge is not None
    assert edge.gross_edge_bps < Decimal(6)
    assert edge.net_edge_bps < Decimal(0)
    assert engine.generate_signal(opportunity, edge) is None


def test_an_unpublished_funding_interval_leaves_the_opportunity_unpriced() -> None:
    engine = strategy()
    engine.on_market_data(
        [
            view(SPOT, "99.99", "100.01"),
            view(PERP, "100.99", "101.01", funding=funding_info(interval=None)),
        ],
        NOW,
    )
    (opportunity,) = engine.detect_opportunities()
    assert engine.calculate_edge(opportunity) is None


def test_an_expired_signal_is_rejected() -> None:
    engine = strategy()
    views = [view(SPOT, "99.99", "100.01"), view(PERP, "100.99", "101.01", funding=funding_info())]
    engine.on_market_data(views, NOW)
    (opportunity,) = engine.detect_opportunities()
    edge = engine.calculate_edge(opportunity)
    assert edge is not None
    signal = engine.generate_signal(opportunity, edge)
    assert signal is not None
    engine.on_market_data(views, NOW + timedelta(seconds=5))  # advance the clock
    result = engine.validate_signal(signal)
    assert not result.is_valid
    assert result.reason is RejectionReason.SIGNAL_EXPIRED


def test_excess_latency_is_rejected() -> None:
    engine = strategy()
    engine.on_market_data(
        [
            view(SPOT, "99.99", "100.01", latency_ms=20),
            view(PERP, "100.99", "101.01", latency_ms=900, funding=funding_info()),
        ],
        NOW,
    )
    (opportunity,) = engine.detect_opportunities()
    edge = engine.calculate_edge(opportunity)
    assert edge is not None
    signal = engine.generate_signal(opportunity, edge)
    assert signal is not None
    result = engine.validate_signal(signal)
    assert not result.is_valid
    assert result.reason is RejectionReason.LATENCY_EXCEEDED


def test_a_trade_under_the_venue_minimum_is_rejected() -> None:
    spec = MarketSpec(
        ref=SPOT,
        base_asset="BTC",
        quote_asset="USDT",
        is_active=True,
        min_notional=Decimal(5000),
    )
    engine = strategy(specs={SPOT: spec})
    engine.on_market_data(
        [view(SPOT, "99.99", "100.01"), view(PERP, "100.99", "101.01", funding=funding_info())],
        NOW,
    )
    (opportunity,) = engine.detect_opportunities()
    edge = engine.calculate_edge(opportunity)
    assert edge is not None
    signal = engine.generate_signal(opportunity, edge)
    assert signal is not None
    result = engine.validate_signal(signal)
    assert not result.is_valid
    assert result.reason is RejectionReason.BELOW_MIN_NOTIONAL


def test_selling_spot_is_refused_without_inventory_or_margin() -> None:
    """The widest live basis needs spot sold short, which a cash account cannot.

    Measured on binance.com: the only pairs clearing costs were sub-cent
    markets whose perpetual traded below spot. Capturing that means selling
    spot, so it is priced and recorded but never signalled.
    """
    engine = strategy()
    engine.on_market_data(
        [view(SPOT, "100.99", "101.01"), view(PERP, "99.99", "100.01", funding=funding_info())],
        NOW,
    )
    (opportunity,) = engine.detect_opportunities()
    assert opportunity.sell.ref is SPOT
    edge = engine.calculate_edge(opportunity)
    assert edge is not None  # still priced: it is research data
    signal = engine.generate_signal(opportunity, edge)
    assert signal is not None
    result = engine.validate_signal(signal)
    assert not result.is_valid
    assert result.reason is RejectionReason.SPOT_SHORT_UNAVAILABLE


def test_selling_spot_is_allowed_when_configured() -> None:
    """A margin account can reach the other direction; it is configuration."""
    engine = strategy(SpotPerpBasisConfig(max_notional_usd=1000.0, allow_spot_short=True))
    engine.on_market_data(
        [view(SPOT, "100.99", "101.01"), view(PERP, "99.99", "100.01", funding=funding_info())],
        NOW,
    )
    (opportunity,) = engine.detect_opportunities()
    edge = engine.calculate_edge(opportunity)
    assert edge is not None
    signal = engine.generate_signal(opportunity, edge)
    assert signal is not None
    assert engine.validate_signal(signal).is_valid


def test_buying_spot_is_always_reachable() -> None:
    """Cash buys spot and sells the perp: the direction a spot account can do."""
    engine = strategy()
    engine.on_market_data(
        [view(SPOT, "99.99", "100.01"), view(PERP, "100.99", "101.01", funding=funding_info())],
        NOW,
    )
    (opportunity,) = engine.detect_opportunities()
    assert opportunity.buy.ref is SPOT
    edge = engine.calculate_edge(opportunity)
    assert edge is not None
    signal = engine.generate_signal(opportunity, edge)
    assert signal is not None
    assert engine.validate_signal(signal).is_valid


def test_using_the_strategy_before_initialize_fails_loudly() -> None:
    engine = SpotPerpBasisStrategy(CONFIG)
    engine.on_market_data(
        [view(SPOT, "99.99", "100.01"), view(PERP, "100.99", "101.01", funding=funding_info())],
        NOW,
    )
    (opportunity,) = engine.detect_opportunities()
    try:
        engine.calculate_edge(opportunity)
    except RuntimeError as exc:
        assert "initialize" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected a RuntimeError")
