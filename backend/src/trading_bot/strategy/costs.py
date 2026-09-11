"""What stands between a gross spread and money kept.

``CostModel`` is the interface a strategy depends on. ``ConfiguredCostModel``
is a **provisional** implementation good enough to stop Phase 5 trading on
gross spread; Phase 6 replaces the implementation, not the interface.

What it already measures rather than assumes:

- slippage, from walking the real order book for the real size (Phase 2's
  ``OrderBook.fill_price``)
- funding, from the venue's live rate *and its actual interval*

What it still assumes, and Phase 6 must fix:

- one flat taker fee per instrument class from configuration, rather than the
  account's fee tier and its maker/taker split
- the exit costs the same as the entry. Closing a converged basis crosses the
  spread again, and charging entry slippage twice is the least dishonest
  estimate available before Phase 8 measures real round trips
- funding accrues linearly over the assumed holding period, and the *current*
  rate persists for it

Funding is signed, not a flat cost: a short perpetual leg receives funding when
the rate is positive. A market whose funding interval the venue does not
publish gets no estimate at all - ``None`` - because guessing eight hours for a
four-hour market understates the cost by half.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Protocol

from trading_bot.core.config import CostsConfig
from trading_bot.db.models.enums import MarketType, Side
from trading_bot.exchange.models import FundingInfo, MarketRef
from trading_bot.strategy.models import CostBreakdown, Edge, Opportunity, to_bps

_HOURS_PER_DAY = Decimal(24)


class CostModel(Protocol):
    """Turns a gross opportunity into a net one, or refuses to guess."""

    def estimate(self, opportunity: Opportunity, funding: FundingInfo | None) -> Edge | None: ...


class ConfiguredCostModel:
    """Fees from configuration, slippage from the book, funding from the venue.

    ``funding_horizon`` is how long the perpetual leg is assumed to be held. It
    matters: over minutes funding is negligible next to fees, and over days it
    dominates. The strategy states its assumption rather than hiding one.
    """

    def __init__(self, config: CostsConfig, *, funding_horizon: timedelta) -> None:
        if funding_horizon < timedelta(0):
            raise ValueError("funding_horizon cannot be negative")
        self._spot_fee_bps = Decimal(str(config.spot_taker_fee_bps))
        self._perp_fee_bps = Decimal(str(config.perp_taker_fee_bps))
        self._buffer_bps = Decimal(str(config.safety_buffer_bps))
        self._horizon = funding_horizon

    @property
    def funding_horizon(self) -> timedelta:
        return self._horizon

    def taker_fee_bps(self, ref: MarketRef) -> Decimal:
        return self._spot_fee_bps if ref.market_type is MarketType.SPOT else self._perp_fee_bps

    def round_trip_fee_bps(self, opportunity: Opportunity) -> Decimal:
        """Taker fees on both legs, twice - entry and exit."""
        legs = sum(
            (self.taker_fee_bps(leg.ref) for leg in opportunity.legs),
            Decimal(0),
        )
        return legs * 2

    def estimate(self, opportunity: Opportunity, funding: FundingInfo | None) -> Edge | None:
        """Net edge, or ``None`` when a cost cannot be estimated honestly."""
        notional = opportunity.notional_usd
        fees_usd = sum(
            (
                leg.notional * self.taker_fee_bps(leg.ref) / Decimal(10_000) * 2
                for leg in opportunity.legs
            ),
            Decimal(0),
        )
        # Entry slippage is measured; the exit is charged the same, since
        # closing crosses the spread again. Phase 8 replaces this with fills.
        entry_slippage = sum((leg.slippage_usd for leg in opportunity.legs), Decimal(0))
        slippage_usd = entry_slippage * 2

        funding_usd = self._funding_cost(opportunity, funding)
        if funding_usd is None:
            return None

        buffer_usd = notional * self._buffer_bps / Decimal(10_000)
        costs = CostBreakdown(
            fees_usd=fees_usd,
            slippage_usd=slippage_usd,
            funding_usd=funding_usd,
            buffer_usd=buffer_usd,
        )
        net_usd = opportunity.gross_edge_usd - costs.total_usd
        return Edge(
            opportunity=opportunity,
            costs=costs,
            net_edge_usd=net_usd,
            net_edge_bps=to_bps(net_usd, notional),
            funding_horizon=self._horizon,
        )

    def _funding_cost(
        self, opportunity: Opportunity, funding: FundingInfo | None
    ) -> Decimal | None:
        """Signed funding over the holding period; ``None`` when unknowable.

        A long perpetual pays when the rate is positive and a short receives,
        so the sign follows the side of the perpetual leg.
        """
        perp = next(
            (leg for leg in opportunity.legs if leg.ref.market_type is not MarketType.SPOT), None
        )
        if perp is None:
            return Decimal(0)  # no perpetual leg, so no funding to pay
        if funding is None:
            return None
        interval_hours = funding.funding_interval_hours
        if interval_hours is None or interval_hours <= 0:
            # Binance omits some symbols from fundingInfo, and their intervals
            # are not all eight hours. Assuming one would halve or double the
            # cost, so the opportunity is refused instead.
            return None
        periods = Decimal(str(self._horizon.total_seconds())) / (
            Decimal(interval_hours) * Decimal(3600)
        )
        paid = perp.notional * funding.last_funding_rate * periods
        return paid if perp.side is Side.BUY else -paid

    def describe(self) -> str:
        """One line for the terminal header, so the assumptions are on screen."""
        hours = Decimal(str(self._horizon.total_seconds())) / Decimal(3600)
        horizon = f"{hours.normalize():f}h" if hours >= 1 else f"{hours * Decimal(60):.0f}m"
        return (
            f"taker {self._spot_fee_bps.normalize():f}/{self._perp_fee_bps.normalize():f} bps "
            f"spot/perp x2 legs x2 sides, slippage from book x2, "
            f"funding over {horizon}, buffer {self._buffer_bps.normalize():f} bps"
        )
