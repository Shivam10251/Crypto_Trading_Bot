"""Spot versus perpetual basis - the first strategy.

Binance quotes the same coin on spot and on a USD-M perpetual. The perpetual
trades at a premium or discount to spot - the *basis* - which funding payments
pull back toward zero. When the basis is wide enough to cover every cost, the
trade is: buy the cheap leg, sell the expensive one, and close on convergence.

Two decisions keep this honest:

**The basis is measured mid-to-mid, but sized against the real book.** Gross
edge is ``perp_mid - spot_mid``; what it costs to actually reach those legs is
slippage, priced by walking the depth for the size we would trade. Netting the
two together would hide how much edge the spreads eat, which is precisely what
research needs to know.

**Both legs must be usable at the same instant.** A basis computed from a live
spot quote and a stale perpetual one is not a discrepancy, it is a measurement
error - and it would look like free money. A pair whose legs are not both LIVE,
both fresh and both backed by a synchronised book produces nothing at all.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import ClassVar

from trading_bot.core.config import SpotPerpBasisConfig
from trading_bot.db.models.enums import MarketType, Side
from trading_bot.exchange.models import FundingInfo, MarketRef, OrderBook
from trading_bot.marketdata.models import BookStatus
from trading_bot.strategy.base import MarketView, Strategy, StrategyContext
from trading_bot.strategy.models import (
    DetectionStats,
    Edge,
    Leg,
    Opportunity,
    RejectionReason,
    Signal,
    ValidationResult,
    to_bps,
)

STRATEGY_NAME = "spot_perp_basis"


@dataclass(frozen=True, slots=True)
class BasisPair:
    """Both legs of one symbol, known to be usable at the same moment."""

    symbol: str
    spot: MarketView
    perpetual: MarketView

    @property
    def spot_mid(self) -> Decimal:
        return _mid(self.spot)

    @property
    def perpetual_mid(self) -> Decimal:
        return _mid(self.perpetual)

    @property
    def basis(self) -> Decimal:
        """Perpetual minus spot: positive when the perpetual is at a premium."""
        return self.perpetual_mid - self.spot_mid

    @property
    def basis_bps(self) -> Decimal:
        return to_bps(self.basis, self.spot_mid)

    @property
    def latency_ms(self) -> int | None:
        """The worse of the two legs - a trade is as slow as its slowest side."""
        latencies = [
            view.snapshot.latency_ms
            for view in (self.spot, self.perpetual)
            if view.snapshot.latency_ms is not None
        ]
        return max(latencies) if latencies else None

    @property
    def age_ms(self) -> int:
        return max(self.spot.snapshot.age_ms or 0, self.perpetual.snapshot.age_ms or 0)


@dataclass
class _Persistence:
    """How long the current basis direction has held for one symbol."""

    direction: Side
    since: datetime


class SpotPerpBasisStrategy(Strategy):
    name: ClassVar[str] = STRATEGY_NAME

    def __init__(self, config: SpotPerpBasisConfig) -> None:
        self._config = config
        self._context: StrategyContext | None = None
        self._pairs: list[BasisPair] = []
        self._now: datetime | None = None
        self._stats = DetectionStats()
        self._persistence: dict[str, _Persistence] = {}
        self._min_net_edge_bps = Decimal(str(config.min_net_edge_bps))
        self._max_notional = Decimal(str(config.max_notional_usd))
        self._ttl = timedelta(milliseconds=config.signal_ttl_ms)

    # --- lifecycle --------------------------------------------------------

    def initialize(self, context: StrategyContext) -> None:
        self._context = context

    def on_market_data(self, views: Sequence[MarketView], now: datetime) -> None:
        """Pair the monitored markets by symbol and keep only usable pairs."""
        self._now = now
        spot: dict[str, MarketView] = {}
        perpetual: dict[str, MarketView] = {}
        for view in views:
            bucket = spot if view.ref.market_type is MarketType.SPOT else perpetual
            bucket[view.ref.symbol] = view

        stats = DetectionStats()
        pairs: list[BasisPair] = []
        for symbol in sorted(spot.keys() & perpetual.keys()):
            stats.pairs_seen += 1
            pair = BasisPair(symbol=symbol, spot=spot[symbol], perpetual=perpetual[symbol])
            reason = self._unusable_reason(pair)
            if reason is not None:
                stats.unusable[reason] += 1
                self._persistence.pop(symbol, None)
                continue
            stats.pairs_usable += 1
            pairs.append(pair)
        self._pairs = pairs
        self._stats = stats

    # --- pipeline ---------------------------------------------------------

    def detect_opportunities(self) -> list[Opportunity]:
        """Every directional basis currently visible, profitable or not."""
        now = self._now
        if now is None:
            return []
        opportunities: list[Opportunity] = []
        for pair in self._pairs:
            opportunity = self._price(pair, now)
            if opportunity is None:
                self._stats.no_basis += 1
                self._persistence.pop(pair.symbol, None)
                continue
            opportunities.append(opportunity)
        return opportunities

    def calculate_edge(self, opportunity: Opportunity) -> Edge | None:
        context = self._require_context()
        funding = self._funding_for(opportunity)
        return context.cost_model.estimate(opportunity, funding)

    def generate_signal(self, opportunity: Opportunity, edge: Edge) -> Signal | None:
        """Only a net edge clearing the configured floor becomes an intent."""
        if edge.net_edge_bps < self._min_net_edge_bps:
            return None
        generated_at = self._now or opportunity.detected_at
        return Signal(
            strategy=self.name,
            generated_at=generated_at,
            opportunity=opportunity,
            edge=edge,
            expected_net_edge_bps=edge.net_edge_bps,
            expires_at=generated_at + self._ttl,
        )

    def validate_signal(self, signal: Signal) -> ValidationResult:
        """Feasibility, re-checked at the last moment before the signal leaves."""
        now = self._now or signal.generated_at
        if signal.is_expired(now):
            return ValidationResult.rejected(
                RejectionReason.SIGNAL_EXPIRED, f"signal expired at {signal.expires_at:%H:%M:%S.%f}"
            )
        opportunity = signal.opportunity
        if not self._config.allow_spot_short and opportunity.sell.ref.market_type is (
            MarketType.SPOT
        ):
            # Measured live: the widest basis on binance.com sits in sub-cent
            # markets whose perpetual trades below spot, and capturing it means
            # selling spot. A cash account cannot, so the opportunity is real
            # and recorded, but it is not a trade this system can place.
            return ValidationResult.rejected(
                RejectionReason.SPOT_SHORT_UNAVAILABLE,
                f"would sell {opportunity.sell.ref.symbol} spot; no inventory or margin",
            )
        if opportunity.latency_ms is not None and (
            opportunity.latency_ms > self._config.max_latency_ms
        ):
            return ValidationResult.rejected(
                RejectionReason.LATENCY_EXCEEDED,
                f"{opportunity.latency_ms} ms > {self._config.max_latency_ms} ms",
            )
        context = self._require_context()
        for leg in opportunity.legs:
            spec = context.spec(leg.ref)
            minimum = spec.min_notional if spec else None
            if minimum is not None and leg.notional < minimum:
                return ValidationResult.rejected(
                    RejectionReason.BELOW_MIN_NOTIONAL,
                    f"{leg.ref.symbol} {leg.notional:.2f} < venue minimum {minimum}",
                )
        return ValidationResult.valid()

    # --- introspection ----------------------------------------------------

    def detection_stats(self) -> DetectionStats:
        return self._stats

    # --- internals --------------------------------------------------------

    def _require_context(self) -> StrategyContext:
        if self._context is None:
            raise RuntimeError("strategy used before initialize()")
        return self._context

    def _unusable_reason(self, pair: BasisPair) -> RejectionReason | None:
        """A basis is only real when both legs are real at the same instant."""
        for view in (pair.spot, pair.perpetual):
            snapshot = view.snapshot
            if not snapshot.is_live or snapshot.mid_price is None:
                return RejectionReason.NOT_LIVE
            if snapshot.book_status is not BookStatus.SYNCED or snapshot.book is None:
                return RejectionReason.BOOK_NOT_SYNCED
            if snapshot.age_ms is not None and snapshot.age_ms > self._config.max_data_age_ms:
                return RejectionReason.STALE_DATA
        return None

    def _price(self, pair: BasisPair, now: datetime) -> Opportunity | None:
        """Turn a basis into a sized, book-priced opportunity."""
        basis = pair.basis
        if basis == 0:
            return None
        # Perpetual at a premium: sell it and buy spot. At a discount: reverse.
        perp_rich = basis > 0
        buy_view, sell_view = (
            (pair.spot, pair.perpetual) if perp_rich else (pair.perpetual, pair.spot)
        )
        buy_book, sell_book = buy_view.snapshot.book, sell_view.snapshot.book
        if buy_book is None or sell_book is None:  # pragma: no cover - guarded above
            return None

        spot_mid = pair.spot_mid
        target = self._max_notional / spot_mid
        quantity = min(
            _fillable(buy_book, Side.BUY, target), _fillable(sell_book, Side.SELL, target)
        )
        if quantity <= 0:
            return None

        buy_price, _ = buy_book.fill_price(Side.BUY, quantity)
        sell_price, _ = sell_book.fill_price(Side.SELL, quantity)
        buy_leg = Leg(
            ref=buy_view.ref,
            side=Side.BUY,
            reference_price=_mid(buy_view),
            executable_price=buy_price,
            quantity=quantity,
            # Unwinding sells this leg back into the bids - the other side of
            # the same book, so the exit is measured rather than assumed.
            exit_price=_unwind_price(buy_book, Side.SELL, quantity),
        )
        sell_leg = Leg(
            ref=sell_view.ref,
            side=Side.SELL,
            reference_price=_mid(sell_view),
            executable_price=sell_price,
            quantity=quantity,
            exit_price=_unwind_price(sell_book, Side.BUY, quantity),
        )
        gross_per_unit = abs(basis)
        direction = Side.BUY if perp_rich else Side.SELL
        return Opportunity(
            strategy=self.name,
            detected_at=now,
            buy=buy_leg,
            sell=sell_leg,
            reference_price=spot_mid,
            quantity=quantity,
            notional_usd=spot_mid * quantity,
            gross_edge_bps=to_bps(gross_per_unit, spot_mid),
            gross_edge_usd=gross_per_unit * quantity,
            liquidity_usd=_pair_liquidity(pair),
            latency_ms=pair.latency_ms,
            duration_ms=self._duration_ms(pair.symbol, direction, now),
        )

    def _duration_ms(self, symbol: str, direction: Side, now: datetime) -> int:
        """How long this direction has held. Resets when the basis flips sign."""
        current = self._persistence.get(symbol)
        if current is None or current.direction is not direction:
            self._persistence[symbol] = _Persistence(direction=direction, since=now)
            return 0
        return int((now - current.since).total_seconds() * 1000)

    def _funding_for(self, opportunity: Opportunity) -> FundingInfo | None:
        perpetual = next(
            (leg.ref for leg in opportunity.legs if leg.ref.market_type is not MarketType.SPOT),
            None,
        )
        if perpetual is None:
            return None
        return self._funding_by_ref().get(perpetual)

    def _funding_by_ref(self) -> dict[MarketRef, FundingInfo]:
        return {
            pair.perpetual.ref: pair.perpetual.funding
            for pair in self._pairs
            if pair.perpetual.funding is not None
        }


def _mid(view: MarketView) -> Decimal:
    """Mid of a market a ``BasisPair`` vouched for; absence is a broken contract."""
    mid = view.snapshot.mid_price
    if mid is None:  # pragma: no cover - _unusable_reason rejects these first
        raise ValueError(f"no mid price for {view.ref}")
    return mid


def _fillable(book: OrderBook, side: Side, quantity: Decimal) -> Decimal:
    """How much of ``quantity`` this book can actually absorb."""
    _, filled = book.fill_price(side, quantity)
    return filled


def _unwind_price(book: OrderBook, side: Side, quantity: Decimal) -> Decimal | None:
    """What closing the position would fetch, or ``None`` if depth runs out.

    Priced against the book we can see now. By the time a basis converges the
    book will have moved, but a measured estimate of the other side beats
    assuming the exit costs whatever the entry did.
    """
    price, filled = book.fill_price(side, quantity)
    return price if filled >= quantity else None


def _pair_liquidity(pair: BasisPair) -> Decimal | None:
    """The thinner leg's resting value near the mid - what bounds the trade."""
    values: list[Decimal] = []
    for view in (pair.spot, pair.perpetual):
        liquidity = view.snapshot.liquidity
        if liquidity is None:
            return None
        values.append(min(liquidity.bid_notional, liquidity.ask_notional))
    return min(values)
