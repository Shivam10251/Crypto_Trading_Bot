"""What stands between a gross spread and money kept.

``CostModel`` is the interface a strategy depends on. Phase 5 shipped a
provisional implementation to stop the strategy trading on gross spread;
this is the real one, and the interface did not have to change.

What Phase 6 measures that Phase 5 assumed:

- **Fees follow the real schedule**, per instrument class and per side of the
  book, with the BNB discount applied at the different rates the two legs get.
  See ``fees.py`` - and note that spot maker equals spot taker, so only the
  perpetual leg rewards resting an order.
- **The exit is walked, not doubled.** Phase 5 charged the entry's slippage
  twice. Unwinding crosses the spread the *other* way - a bought leg is sold
  into the bids - which is a different walk of the same book, and the strategy
  now prices it.
- **Funding is discrete.** It settles at fixed times, so a position pays only
  if it is held across one. Measured on binance.com: a 60-minute BTC hold
  starting at 16:51 UTC crosses **zero** settlements, while a continuous model
  charges 0.125 of one - a cost that would never actually be paid.

What it still assumes, and what would have to change it:

- The account's VIP tier comes from configuration. Binance only reports real
  rates behind an authenticated endpoint, and a venue that does publish them
  overrides the configured guess.
- The exit is priced against *today's* book. By the time a basis converges the
  book will have moved; this is the best estimate available before Phase 8
  measures real round trips.
- The current funding rate is assumed to hold for each settlement crossed.
- A maker fill is assumed to happen when the role says maker. Whether a resting
  order is actually hit is an execution question for Phase 8, which is why the
  default is taker on both sides.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Protocol

from trading_bot.core.config import CostsConfig
from trading_bot.db.models.enums import MarketType, Side
from trading_bot.exchange.models import BPS_SCALE, FundingInfo
from trading_bot.strategy.fees import FeeSchedule, OrderRole
from trading_bot.strategy.models import CostBreakdown, Edge, Leg, Opportunity, to_bps


class CostModel(Protocol):
    """Turns a gross opportunity into a net one, or refuses to guess."""

    def estimate(self, opportunity: Opportunity, funding: FundingInfo | None) -> Edge | None: ...


def settlements_crossed(funding: FundingInfo, start: datetime, horizon: timedelta) -> int | None:
    """How many funding settlements fall inside ``[start, start + horizon]``.

    This is the correction that matters most. Funding is not a rate accruing
    by the second: it is a payment at fixed times, and a position that opens
    and closes between two of them pays nothing at all. Holding a 4-hour market
    for one hour costs either a full settlement or none, depending entirely on
    when the hour starts.

    ``None`` when the interval is unknown, because then neither the count nor
    the schedule can be worked out.
    """
    interval_hours = funding.funding_interval_hours
    if interval_hours is None or interval_hours <= 0:
        return None
    interval = timedelta(hours=interval_hours)
    end = start + horizon
    settlement = funding.next_funding_time
    # A schedule already behind us: wind forward to the next one still ahead.
    if settlement < start:
        missed = (start - settlement) // interval + 1
        settlement += missed * interval
    crossed = 0
    while settlement <= end:
        crossed += 1
        settlement += interval
    return crossed


class TransactionCostModel:
    """Fees from the schedule, slippage from the book, funding from the clock."""

    def __init__(self, config: CostsConfig, *, fees: FeeSchedule | None = None) -> None:
        self._config = config
        self._fees = fees or FeeSchedule.from_config(config)
        self._buffer_bps = Decimal(str(config.safety_buffer_bps))
        self._horizon = timedelta(minutes=config.funding_horizon_minutes)
        self._entry_role = OrderRole(config.entry_role.upper())
        self._exit_role = OrderRole(config.exit_role.upper())

    @property
    def fees(self) -> FeeSchedule:
        return self._fees

    @property
    def funding_horizon(self) -> timedelta:
        return self._horizon

    def round_trip_fee_bps(self, opportunity: Opportunity) -> Decimal:
        """Both legs, entry and exit, at their configured roles."""
        return sum(
            (
                self._fees.rate_bps(leg.ref, self._entry_role)
                + self._fees.rate_bps(leg.ref, self._exit_role)
                for leg in opportunity.legs
            ),
            Decimal(0),
        )

    def estimate(self, opportunity: Opportunity, funding: FundingInfo | None) -> Edge | None:
        """Net edge, or ``None`` when a cost cannot be estimated honestly."""
        fees_usd = sum(
            (self._leg_fees(leg) for leg in opportunity.legs),
            Decimal(0),
        )
        slippage_usd = sum(
            (self._leg_slippage(leg) for leg in opportunity.legs),
            Decimal(0),
        )
        funding_usd = self._funding_cost(opportunity, funding)
        if funding_usd is None:
            return None

        notional = opportunity.notional_usd
        buffer_usd = notional * self._buffer_bps / BPS_SCALE
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

    # --- components -------------------------------------------------------

    def _leg_fees(self, leg: Leg) -> Decimal:
        """Entry and exit are charged separately; the roles can differ."""
        entry = self._fees.rate_bps(leg.ref, self._entry_role) / BPS_SCALE * leg.notional
        # The exit trades the same quantity at whatever it can be unwound for.
        exit_notional = (leg.exit_price or leg.executable_price) * leg.quantity
        exit_fee = self._fees.rate_bps(leg.ref, self._exit_role) / BPS_SCALE * exit_notional
        return entry + exit_fee

    def _leg_slippage(self, leg: Leg) -> Decimal:
        """Measured on the way in, and measured again on the way out.

        Falls back to charging the entry twice only when the book could not
        price the unwind - which is the Phase 5 assumption, kept as the
        conservative answer rather than dropping the cost entirely.
        """
        entry = leg.slippage_usd
        measured_exit = leg.exit_slippage_usd
        return entry + (measured_exit if measured_exit is not None else entry)

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
        crossed = settlements_crossed(funding, opportunity.detected_at, self._horizon)
        if crossed is None:
            # Binance omits some symbols from fundingInfo, and their intervals
            # are not all eight hours. Assuming one would halve or double the
            # cost, so the opportunity is refused instead.
            return None
        paid = perp.notional * funding.last_funding_rate * crossed
        if paid == 0:
            # Negating a zero Decimal yields -0, which prints as "-0.00" and
            # reads like a rounding artefact rather than "no settlement".
            return Decimal(0)
        return paid if perp.side is Side.BUY else -paid

    # --- reporting --------------------------------------------------------

    def describe(self) -> str:
        """One line for the terminal header, so the assumptions are on screen."""
        hours = Decimal(str(self._horizon.total_seconds())) / Decimal(3600)
        horizon = f"{hours.normalize():f}h" if hours >= 1 else f"{hours * Decimal(60):.0f}m"
        return (
            f"{self._fees.describe()}; "
            f"{self._entry_role.value.lower()} in / {self._exit_role.value.lower()} out, "
            f"slippage walked both ways, funding at settlements crossed in {horizon}, "
            f"buffer {self._buffer_bps.normalize():f} bps"
        )
