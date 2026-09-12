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

What Phase 8's prerequisite remediation corrected on top of that:

- **Funding is charged on the mark notional**, not on what the perpetual leg
  cost to enter. The venue settles ``mark price x position size x rate``; the
  entry's executable price includes the spread we crossed, which the venue
  does not charge funding on.
- **The settlement window is half-open**, ``(start, start + horizon]``, so a
  fresh ``nextFundingTime`` and a stale one describing the same schedule give
  the same count. They previously did not.
- **An unwind the book cannot fill has no price**, so the opportunity is
  refused rather than charged the entry's slippage twice. Missing exit
  liquidity can be far worse than entry liquidity; doubling the entry is a
  guess wearing the word "conservative".
- **Venue-reported fees reach the calculation.** ``FeeSchedule`` could always
  prefer them; nothing was passing it the ``MarketSpec`` that carries them.

What it still assumes, and what would have to change it:

- The account's VIP tier comes from configuration. Binance only reports real
  rates behind an authenticated endpoint, and a venue that does publish them
  overrides the configured guess.
- The exit is priced against *today's* book. By the time a basis converges the
  book will have moved; this is the best estimate available before Phase 8
  measures real round trips.
- The current funding rate is assumed to hold for each settlement crossed, and
  that assumption is stored on the row whenever more than one is charged.
- The gross edge assumes the basis converges to ``assumed_terminal_basis_bps``
  (default 0). That is a statement about the future, recorded as one.
- A maker fill is assumed to happen when the role says maker. Whether a resting
  order is actually hit is an execution question for Phase 8, which is why the
  default is taker on both sides.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
from decimal import ROUND_CEILING, Decimal
from typing import Any, Protocol

from trading_bot.core.config import CostsConfig
from trading_bot.db.models.enums import MarketType, Side
from trading_bot.exchange.models import BPS_SCALE, FundingInfo, MarketRef, MarketSpec
from trading_bot.strategy.evidence import FundingEvidence, LegFeeEvidence, PricingEvidence
from trading_bot.strategy.fees import FeeSchedule, OrderRole
from trading_bot.strategy.models import (
    CostBreakdown,
    Edge,
    Leg,
    Opportunity,
    PricingRefusal,
    PricingResult,
    RejectionReason,
    to_bps,
)

#: Bumped whenever the arithmetic changes, and stored on every row priced by
#: it, so a later comparison knows whether two rows are commensurable.
COST_MODEL_VERSION = "2026.09.12"


class CostModel(Protocol):
    """Turns a gross opportunity into a net one, or says why it will not."""

    def estimate(self, opportunity: Opportunity, funding: FundingInfo | None) -> PricingResult: ...


def settlements_crossed(funding: FundingInfo, start: datetime, horizon: timedelta) -> int | None:
    """Funding settlements in the half-open window ``(start, start + horizon]``.

    Funding is not a rate accruing by the second: it is a payment at fixed
    times, and a position that opens and closes between two of them pays
    nothing at all. Holding a 4-hour market for one hour costs either a full
    settlement or none, depending entirely on when the hour starts.

    **The window is half-open at the start and closed at the end**, which is
    the convention a position has to obey: opening exactly *at* a settlement
    means we were not holding across it and do not pay it, while still holding
    at the closing settlement means we do. Making the start exclusive is also
    what makes the count independent of how the same schedule is described -
    ``nextFundingTime = T`` and a stale ``T - interval`` now agree, where
    before the first charged a settlement the second did not.

    ``None`` when the interval is unknown, because then neither the count nor
    the schedule can be worked out.
    """
    interval_hours = funding.funding_interval_hours
    if interval_hours is None or interval_hours <= 0:
        return None
    interval = timedelta(hours=interval_hours)
    end = start + horizon
    settlement = funding.next_funding_time
    # Wind to the first settlement strictly after the open, whether the venue
    # gave us a future time or one the clock has already passed.
    if settlement <= start:
        steps = (start - settlement) // interval + 1
        settlement += steps * interval
    crossed = 0
    while settlement <= end:
        crossed += 1
        settlement += interval
    return crossed


class TransactionCostModel:
    """Fees from the schedule, slippage from the book, funding from the clock."""

    def __init__(
        self,
        config: CostsConfig,
        *,
        fees: FeeSchedule | None = None,
        specs: Mapping[MarketRef, MarketSpec] | None = None,
    ) -> None:
        self._config = config
        self._fees = fees or FeeSchedule.from_config(config)
        # Reference data per market, so a venue-reported fee rate reaches the
        # calculation instead of only the schedule that knows how to use one.
        self._specs = dict(specs or {})
        self._buffer_bps = Decimal(str(config.safety_buffer_bps))
        self._horizon = timedelta(minutes=config.funding_horizon_minutes)
        self._entry_role = OrderRole(config.entry_role.upper())
        self._exit_role = OrderRole(config.exit_role.upper())
        self._terminal_basis_bps = Decimal(str(config.assumed_terminal_basis_bps))
        self._borrow_rate_bps = (
            Decimal(str(config.spot_borrow_rate_bps_per_day))
            if config.spot_borrow_rate_bps_per_day is not None
            else None
        )
        self._borrow_rounding_hours = config.spot_borrow_rounding_hours

    @property
    def fees(self) -> FeeSchedule:
        return self._fees

    def _rate_bps(self, ref: MarketRef, role: OrderRole) -> Decimal:
        return self._fees.rate_bps(ref, role, self._specs.get(ref))

    def _venue_reported(self, ref: MarketRef) -> bool:
        spec = self._specs.get(ref)
        if spec is None:
            return False
        return spec.maker_fee_bps is not None or spec.taker_fee_bps is not None

    @property
    def funding_horizon(self) -> timedelta:
        return self._horizon

    def round_trip_fee_bps(self, opportunity: Opportunity) -> Decimal:
        """Both legs, entry and exit, at their configured roles."""
        return sum(
            (
                self._rate_bps(leg.ref, self._entry_role) + self._rate_bps(leg.ref, self._exit_role)
                for leg in opportunity.legs
            ),
            Decimal(0),
        )

    def estimate(self, opportunity: Opportunity, funding: FundingInfo | None) -> PricingResult:
        """Net edge, or the reason a cost could not be estimated honestly."""
        spot_short = next(
            (
                leg
                for leg in opportunity.legs
                if leg.ref.market_type is MarketType.SPOT and leg.side is Side.SELL
            ),
            None,
        )
        if spot_short is not None and self._borrow_rate_bps is None:
            return PricingRefusal(
                RejectionReason.BORROW_COST_UNKNOWN,
                f"no account borrow rate configured for {spot_short.ref.symbol}",
            )
        unpriceable = [leg.ref.symbol for leg in opportunity.legs if leg.unwind_price is None]
        if unpriceable:
            # The opposite side of the book could not absorb the position we
            # would have to close. There is no exit price, so there is no
            # round trip to price - and charging the entry again would invent
            # a number that says the exit is as cheap as the entry, which is
            # exactly the case where it is not.
            return PricingRefusal(
                reason=RejectionReason.UNWIND_NOT_FILLABLE,
                detail=f"no depth to unwind {', '.join(unpriceable)} at the full quantity",
            )

        perp = next(
            (leg for leg in opportunity.legs if leg.ref.market_type is not MarketType.SPOT), None
        )
        crossed = 0
        if perp is not None:
            if funding is None:
                return PricingRefusal(
                    RejectionReason.FUNDING_UNKNOWN,
                    f"no funding observation for {perp.ref.symbol}",
                )
            settlements = settlements_crossed(funding, opportunity.detected_at, self._horizon)
            if settlements is None:
                # Binance omits some symbols from fundingInfo, and their
                # intervals are not all eight hours. Assuming one would halve
                # or double the cost, so the opportunity is refused instead.
                return PricingRefusal(
                    RejectionReason.FUNDING_UNKNOWN,
                    f"{perp.ref.symbol} publishes no funding interval",
                )
            crossed = settlements

        fees_usd = sum((self._leg_fees(leg) for leg in opportunity.legs), Decimal(0))
        slippage_usd = sum((self._leg_slippage(leg) for leg in opportunity.legs), Decimal(0))
        funding_usd = self._funding_cost(perp, funding, crossed)
        borrow_usd = self._borrow_cost(spot_short)

        notional = opportunity.notional_usd
        buffer_usd = notional * self._buffer_bps / BPS_SCALE
        costs = CostBreakdown(
            fees_usd=fees_usd,
            slippage_usd=slippage_usd,
            funding_usd=funding_usd,
            buffer_usd=buffer_usd,
            # The basis is assumed to converge to this, not to zero. Zero by
            # default, so the assumption is visible without being a thumb on
            # the scale.
            other_usd=notional * self._terminal_basis_bps / BPS_SCALE,
            borrow_usd=borrow_usd,
        )
        net_usd = opportunity.gross_edge_usd - costs.total_usd
        return Edge(
            opportunity=opportunity,
            costs=costs,
            net_edge_usd=net_usd,
            net_edge_bps=to_bps(net_usd, notional),
            funding_horizon=self._horizon,
            pricing=PricingEvidence(
                cost_model_version=COST_MODEL_VERSION,
                assumptions=self.assumptions(),
                fees=tuple(self._leg_fee_evidence(leg) for leg in opportunity.legs),
                funding=(
                    None
                    if perp is None or funding is None
                    else FundingEvidence.of(
                        funding,
                        age_ms=int(
                            (opportunity.detected_at - funding.local_timestamp).total_seconds()
                            * 1000
                        ),
                        settlements=crossed,
                    )
                ),
            ),
        )

    # --- components -------------------------------------------------------

    def _leg_fees(self, leg: Leg) -> Decimal:
        """Entry and exit are charged separately; the roles can differ."""
        entry = self._rate_bps(leg.ref, self._entry_role) / BPS_SCALE * leg.notional
        # The exit trades the same quantity at what it can be unwound for -
        # a price that exists, because ``estimate`` refuses when it does not.
        exit_notional = leg.unwind_notional or leg.notional
        exit_fee = self._rate_bps(leg.ref, self._exit_role) / BPS_SCALE * exit_notional
        return entry + exit_fee

    def _leg_fee_evidence(self, leg: Leg) -> LegFeeEvidence:
        return LegFeeEvidence(
            symbol=leg.ref.symbol,
            market_type=leg.ref.market_type.value,
            entry_role=self._entry_role.value,
            entry_rate_bps=self._rate_bps(leg.ref, self._entry_role),
            exit_role=self._exit_role.value,
            exit_rate_bps=self._rate_bps(leg.ref, self._exit_role),
            venue_reported=self._venue_reported(leg.ref),
        )

    def _leg_slippage(self, leg: Leg) -> Decimal:
        """Measured on the way in, and measured again on the way out.

        Both walks exist by the time this runs: ``estimate`` refuses an
        opportunity whose unwind the book could not fill, rather than charging
        the entry twice and calling the guess conservative.
        """
        unwind = leg.unwind_slippage_usd
        return leg.slippage_usd + (unwind if unwind is not None else Decimal(0))

    def _funding_cost(
        self, perp: Leg | None, funding: FundingInfo | None, settlements: int
    ) -> Decimal:
        """Signed funding over the holding period.

        Charged on the **mark** notional, which is what the venue settles
        against: ``mark price x position size x rate``, per settlement crossed.
        The entry's executable price is what we paid to cross the spread, and
        the venue does not charge funding on that.

        A long perpetual pays when the rate is positive and a short receives,
        so the sign follows the side of the perpetual leg.
        """
        if perp is None or funding is None:
            return Decimal(0)  # no perpetual leg, so no funding to pay
        paid = funding.mark_price * perp.quantity * funding.last_funding_rate * settlements
        if paid == 0:
            # Negating a zero Decimal yields -0, which prints as "-0.00" and
            # reads like a rounding artefact rather than "no settlement".
            return Decimal(0)
        return paid if perp.side is Side.BUY else -paid

    def _borrow_cost(self, spot_short: Leg | None) -> Decimal:
        if spot_short is None or self._borrow_rate_bps is None:
            return Decimal(0)
        hours = Decimal(str(self._horizon.total_seconds())) / Decimal(3600)
        increments = (hours / Decimal(self._borrow_rounding_hours)).to_integral_value(
            rounding=ROUND_CEILING
        )
        charged_days = increments * Decimal(self._borrow_rounding_hours) / Decimal(24)
        return spot_short.notional * self._borrow_rate_bps / BPS_SCALE * charged_days

    # --- reporting --------------------------------------------------------

    def assumptions(self) -> dict[str, Any]:
        """Everything the number depends on that is not measured, as data.

        Stored with each opportunity so a row priced today stays interpretable
        after the configuration behind it has changed.
        """
        return {
            "entry_role": self._entry_role.value,
            "exit_role": self._exit_role.value,
            "spot_maker_bps": format(self._fees.spot.maker_bps, "f"),
            "spot_taker_bps": format(self._fees.spot.taker_bps, "f"),
            "perp_maker_bps": format(self._fees.perpetual.maker_bps, "f"),
            "perp_taker_bps": format(self._fees.perpetual.taker_bps, "f"),
            "pays_fees_in_bnb": self._fees.pays_in_bnb,
            "safety_buffer_bps": format(self._buffer_bps, "f"),
            "funding_horizon_minutes": self._horizon.total_seconds() / 60,
            "settlement_window": "(open, open + horizon]",
            # The gross edge is what convergence to this basis would be worth.
            # It is an assumption about the future, not realised profit: actual
            # price P&L is signed_quantity x (entry basis - exit basis), which
            # only Phase 8's fills can measure.
            "assumed_terminal_basis_bps": format(self._terminal_basis_bps, "f"),
            "spot_borrow_rate_bps_per_day": (
                format(self._borrow_rate_bps, "f") if self._borrow_rate_bps is not None else None
            ),
            "spot_borrow_rounding_hours": self._borrow_rounding_hours,
            "gross_edge_is": "theoretical convergence edge, not realised profit",
        }

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
