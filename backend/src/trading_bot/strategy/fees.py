"""What the venue charges, per instrument class and per side of the book.

Phase 5 modelled fees as one flat taker rate per instrument class. That was
enough to show the basis does not survive costs, but not enough to say what the
floor actually is - and the floor is the question, because it decides whether
any cost improvement could make this strategy work.

Three things the real schedule has that a flat rate does not:

- **Maker and taker differ, but not everywhere.** On binance.com spot at VIP 0
  both are 0.100%, so resting a limit order on the spot leg saves nothing at
  all. On USD-M perpetuals maker is 0.020% against 0.050% taker, so the
  perpetual leg is the only one where patience is worth anything.
- **Paying fees in BNB discounts them**, by 25% on spot and 10% on futures -
  different rates, applied to different legs of the same trade.
- **The rates are per account, not per venue.** They follow the VIP tier and
  are only readable with credentials, so they are configuration here, with the
  published VIP 0 schedule as the default.

Nothing in this module knows whether an order will actually fill as a maker.
Charging the maker rate assumes a resting order was hit, which is an execution
question that Phase 8 answers - so the role is configuration, stated on screen,
and never quietly assumed to be the favourable one.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from trading_bot.core.config import CostsConfig
from trading_bot.db.models.enums import MarketType
from trading_bot.exchange.models import MarketRef, MarketSpec

_PERCENT = Decimal(100)


class OrderRole(StrEnum):
    """Which side of the book an order was on when it filled."""

    MAKER = "MAKER"  # rested and was hit
    TAKER = "TAKER"  # crossed the spread


@dataclass(frozen=True, slots=True)
class InstrumentFees:
    """One instrument class's rates, after any discount."""

    maker_bps: Decimal
    taker_bps: Decimal

    def rate(self, role: OrderRole) -> Decimal:
        return self.maker_bps if role is OrderRole.MAKER else self.taker_bps

    def discounted(self, percent: Decimal) -> InstrumentFees:
        factor = (_PERCENT - percent) / _PERCENT
        return InstrumentFees(maker_bps=self.maker_bps * factor, taker_bps=self.taker_bps * factor)


@dataclass(frozen=True, slots=True)
class FeeSchedule:
    """The account's rates for both legs of a spot/perpetual trade."""

    spot: InstrumentFees
    perpetual: InstrumentFees
    pays_in_bnb: bool = False

    @classmethod
    def from_config(cls, config: CostsConfig) -> FeeSchedule:
        spot = InstrumentFees(
            maker_bps=Decimal(str(config.spot_maker_fee_bps)),
            taker_bps=Decimal(str(config.spot_taker_fee_bps)),
        )
        perpetual = InstrumentFees(
            maker_bps=Decimal(str(config.perp_maker_fee_bps)),
            taker_bps=Decimal(str(config.perp_taker_fee_bps)),
        )
        if config.pay_fees_in_bnb:
            # Different discounts on the two legs of the same trade.
            spot = spot.discounted(Decimal(str(config.bnb_discount_spot_pct)))
            perpetual = perpetual.discounted(Decimal(str(config.bnb_discount_futures_pct)))
        return cls(spot=spot, perpetual=perpetual, pays_in_bnb=config.pay_fees_in_bnb)

    def for_market(self, ref: MarketRef) -> InstrumentFees:
        return self.spot if ref.market_type is MarketType.SPOT else self.perpetual

    def rate_bps(self, ref: MarketRef, role: OrderRole, spec: MarketSpec | None = None) -> Decimal:
        """Rate for one fill. A venue-reported rate wins over configuration.

        Binance only reports fees behind an authenticated endpoint, so in
        practice ``spec`` carries None and configuration decides - but a venue
        that does publish them must not be overridden by a guess.
        """
        if spec is not None:
            reported = spec.maker_fee_bps if role is OrderRole.MAKER else spec.taker_fee_bps
            if reported is not None:
                return reported
        return self.for_market(ref).rate(role)

    def describe(self) -> str:
        bnb = " (BNB discount applied)" if self.pays_in_bnb else ""
        return (
            f"spot {_fmt(self.spot.maker_bps)}/{_fmt(self.spot.taker_bps)} "
            f"perp {_fmt(self.perpetual.maker_bps)}/{_fmt(self.perpetual.taker_bps)} "
            f"bps maker/taker{bnb}"
        )


def _fmt(value: Decimal) -> str:
    return f"{value.normalize():f}"
