"""P&L arithmetic. Pure Decimal; no database, no clock, no market data.

Everything here is computed from **actual fills**. A strategy's estimated
price never enters: it is what the system hoped for, and the whole point of
Phase 8 was to find out that the two differ.

## Sign conventions

``s = +1`` for a position entered BUY (long) and ``s = -1`` for one entered
SELL (short). With weighted entry price ``P_in``, weighted exit price
``P_out`` and closed quantity ``Q_out``:

```
price P&L          = s * (P_out - P_in) * Q_out
                   = s * (N_out - P_in * Q_out)
fees on the close  = F_in * (Q_out / Q_in) + F_out
realized net P&L   = price P&L - fees on the close
                     + funding    (only when measured)
                     - borrow     (only when measured)
unrealized P&L     = s * (mark - P_in) * (Q_in - Q_out)
gross exposure     = P_in * (Q_in - Q_out)          (always >= 0)
net exposure       = s * P_in * (Q_in - Q_out)
```

Four rules the formulas above encode, each of which is a way P&L is usually
got wrong:

1. **Fees are subtracted exactly once.** Entry fees are prorated by the
   closed fraction, so the sum of every partial close's fee charge is exactly
   the entry fee, and the exit's own fee is charged on the close that paid it.
2. **Slippage is never subtracted.** It is already inside ``P_in`` and
   ``P_out`` - those are fill prices, not quotes. It is carried as attribution
   (``slippage_usd``) so the cost model can be checked, and subtracting it
   again would double-count the one cost the fills already paid.
3. **An unmeasured cash flow is not zero.** Funding and spot borrow are real
   and nothing in this system measures either yet, so they are ``None`` and
   named in ``unmeasured``. A realized figure missing them is never called a
   total.
4. **An open or partially closed position is not realized.** ``realized``
   covers ``Q_out`` only; the remainder is unrealized, or nothing at all when
   there is no honest mark.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from trading_bot.db.models.enums import Side

ZERO = Decimal(0)

#: Cash-flow components this system cannot measure from the data it stores.
FUNDING = "funding"
SPOT_BORROW = "spot_borrow"


@dataclass(frozen=True, slots=True)
class FillLot:
    """One execution, as the accounting needs it.

    ``expected_price`` is what the strategy thought it would get, kept only so
    realised slippage can be attributed. It never enters a P&L figure.
    """

    price: Decimal
    quantity: Decimal
    fee_usd: Decimal
    filled_at: datetime
    expected_price: Decimal | None = None

    @property
    def notional(self) -> Decimal:
        return self.price * self.quantity


@dataclass(frozen=True, slots=True)
class Weighted:
    """A set of fills reduced to one weighted price and its costs."""

    quantity: Decimal
    notional: Decimal
    fees_usd: Decimal
    slippage_usd: Decimal
    first_at: datetime | None
    last_at: datetime | None

    @property
    def price(self) -> Decimal | None:
        """``None`` for an empty set: there is no price for zero size."""
        if self.quantity <= 0:
            return None
        return self.notional / self.quantity


def weigh(side: Side, lots: Sequence[FillLot]) -> Weighted:
    """Reduce fills to a weighted price, their fees and their slippage.

    Slippage is signed against the expected price, adverse-positive: a buy
    filled above expectation and a sell filled below it both read positive.
    A fill that came in better than expected reads negative rather than being
    floored at zero - the distribution of that error is what says whether the
    cost model can be trusted, and flooring it would hide half of it.
    """
    quantity = sum((lot.quantity for lot in lots), ZERO)
    notional = sum((lot.notional for lot in lots), ZERO)
    fees = sum((lot.fee_usd for lot in lots), ZERO)
    slippage = ZERO
    for lot in lots:
        if lot.expected_price is None or lot.expected_price <= 0:
            continue
        difference = (
            lot.price - lot.expected_price if side is Side.BUY else lot.expected_price - lot.price
        )
        slippage += difference * lot.quantity
    times = [lot.filled_at for lot in lots]
    return Weighted(
        quantity=quantity,
        notional=notional,
        fees_usd=fees,
        slippage_usd=slippage,
        first_at=min(times) if times else None,
        last_at=max(times) if times else None,
    )


class TradeOutcome(StrEnum):
    """What a completed paired trade did, net of every measured cost."""

    WIN = "WIN"
    LOSS = "LOSS"
    BREAKEVEN = "BREAKEVEN"


@dataclass(frozen=True, slots=True)
class PositionPnl:
    """One leg's accounting, from its own fills.

    A leg is never a trade on its own: a basis attempt's win or loss is the
    net of both legs, and asking whether the spot leg "won" is asking a
    question the strategy never posed.
    """

    side: Side
    entry: Weighted
    exit: Weighted
    #: Sum of both sides' fees. Entry fees are prorated onto the closed
    #: portion by ``fees_on_closed``; this is the lifetime total.
    fees_usd: Decimal
    slippage_usd: Decimal
    price_pnl_usd: Decimal
    fees_on_closed_usd: Decimal
    realized_pnl_usd: Decimal
    unrealized_pnl_usd: Decimal | None
    gross_exposure_usd: Decimal
    net_exposure_usd: Decimal
    funding_pnl_usd: Decimal | None
    borrow_cost_usd: Decimal | None
    unmeasured: tuple[str, ...]
    holding: timedelta | None
    mark_price: Decimal | None

    @property
    def sign(self) -> Decimal:
        return Decimal(1) if self.side is Side.BUY else Decimal(-1)

    @property
    def opened_quantity(self) -> Decimal:
        return self.entry.quantity

    @property
    def closed_quantity(self) -> Decimal:
        return self.exit.quantity

    @property
    def open_quantity(self) -> Decimal:
        return self.entry.quantity - self.exit.quantity

    @property
    def is_flat(self) -> bool:
        """Every opened unit has been given back."""
        return self.entry.quantity > 0 and self.open_quantity <= 0

    @property
    def is_partially_closed(self) -> bool:
        return self.exit.quantity > 0 and not self.is_flat

    @property
    def realized_is_complete(self) -> bool:
        return not self.unmeasured


def position_pnl(
    *,
    side: Side,
    entries: Sequence[FillLot],
    exits: Sequence[FillLot],
    mark_price: Decimal | None = None,
    funding_pnl_usd: Decimal | None = None,
    borrow_cost_usd: Decimal | None = None,
    borrows: bool = False,
) -> PositionPnl:
    """One leg's P&L from its fills. ``side`` is the side it was **entered** on.

    ``borrows`` says this leg is held on borrowed inventory (a short spot
    leg), which makes an unmeasured borrow cost a named gap rather than an
    irrelevance - a long spot leg borrows nothing and is not missing anything
    by having no borrow figure.
    """
    entry = weigh(side, entries)
    exit_side = Side.SELL if side is Side.BUY else Side.BUY
    closed = weigh(exit_side, exits)
    if closed.quantity > entry.quantity:
        raise ValueError(
            f"closed quantity {closed.quantity} exceeds opened {entry.quantity}: "
            "a close may only reduce a position"
        )
    entry_price = entry.price
    sign = Decimal(1) if side is Side.BUY else Decimal(-1)

    if entry_price is None:
        price_pnl = ZERO
        fees_on_closed = closed.fees_usd
    else:
        price_pnl = sign * (closed.notional - entry_price * closed.quantity)
        prorated_entry_fees = (
            entry.fees_usd * closed.quantity / entry.quantity if entry.quantity > 0 else ZERO
        )
        fees_on_closed = prorated_entry_fees + closed.fees_usd

    unmeasured: list[str] = []
    realized = price_pnl - fees_on_closed
    if funding_pnl_usd is None:
        unmeasured.append(FUNDING)
    else:
        realized += funding_pnl_usd
    if borrows:
        if borrow_cost_usd is None:
            unmeasured.append(SPOT_BORROW)
        else:
            realized -= borrow_cost_usd

    open_quantity = entry.quantity - closed.quantity
    unrealized = (
        sign * (mark_price - entry_price) * open_quantity
        if mark_price is not None and entry_price is not None and open_quantity > 0
        else (ZERO if open_quantity <= 0 else None)
    )
    gross = entry_price * open_quantity if entry_price is not None else ZERO
    holding = (
        closed.last_at - entry.first_at
        if closed.last_at is not None and entry.first_at is not None and open_quantity <= 0
        else None
    )
    return PositionPnl(
        side=side,
        entry=entry,
        exit=closed,
        fees_usd=entry.fees_usd + closed.fees_usd,
        slippage_usd=entry.slippage_usd + closed.slippage_usd,
        price_pnl_usd=price_pnl,
        fees_on_closed_usd=fees_on_closed,
        realized_pnl_usd=realized,
        unrealized_pnl_usd=unrealized,
        gross_exposure_usd=gross,
        net_exposure_usd=sign * gross,
        funding_pnl_usd=funding_pnl_usd,
        borrow_cost_usd=borrow_cost_usd,
        unmeasured=tuple(unmeasured),
        holding=holding,
        mark_price=mark_price,
    )


@dataclass(frozen=True, slots=True)
class PairedTrade:
    """Both legs of one basis attempt - the unit a win or a loss is decided on.

    A basis trade is two orders that only mean anything together: the spot leg
    losing exactly what the perpetual leg made is the *intended* outcome, and
    scoring the legs separately would report one win and one loss for a trade
    that netted zero.
    """

    attempt_id: str
    strategy: str
    legs: tuple[PositionPnl, ...]
    opened_at: datetime | None
    closed_at: datetime | None

    @property
    def is_complete(self) -> bool:
        """Exactly two legs are flat. Only then is this a paired result."""
        return len(self.legs) == 2 and all(leg.is_flat for leg in self.legs)

    @property
    def is_unpaired(self) -> bool:
        """One leg still carries exposure while another has none left.

        Real residual risk: the hedge no longer exists, whether because a leg
        never filled or because closing it succeeded and its partner did not.
        """
        if not self.legs or all(leg.is_flat for leg in self.legs):
            return False
        if len(self.legs) != 2:
            return True
        live = [leg for leg in self.legs if not leg.is_flat]
        if len(live) != 2:
            return True
        remaining = [leg.entry.quantity - leg.exit.quantity for leg in live]
        return remaining[0] != remaining[1]

    @property
    def realized_pnl_usd(self) -> Decimal:
        return sum((leg.realized_pnl_usd for leg in self.legs), ZERO)

    @property
    def price_pnl_usd(self) -> Decimal:
        return sum((leg.price_pnl_usd for leg in self.legs), ZERO)

    @property
    def fees_usd(self) -> Decimal:
        return sum((leg.fees_on_closed_usd for leg in self.legs), ZERO)

    @property
    def slippage_usd(self) -> Decimal:
        return sum((leg.slippage_usd for leg in self.legs), ZERO)

    @property
    def unrealized_pnl_usd(self) -> Decimal | None:
        """``None`` when any open leg could not be marked honestly."""
        total = ZERO
        for leg in self.legs:
            if leg.unrealized_pnl_usd is None:
                return None
            total += leg.unrealized_pnl_usd
        return total

    @property
    def unmeasured(self) -> tuple[str, ...]:
        seen: list[str] = []
        for leg in self.legs:
            for component in leg.unmeasured:
                if component not in seen:
                    seen.append(component)
        return tuple(seen)

    @property
    def holding(self) -> timedelta | None:
        if self.opened_at is None or self.closed_at is None:
            return None
        return self.closed_at - self.opened_at

    @property
    def outcome(self) -> TradeOutcome | None:
        """``None`` until the whole attempt is closed - never a partial score."""
        if not self.is_complete:
            return None
        net = self.realized_pnl_usd
        if net > 0:
            return TradeOutcome.WIN
        if net < 0:
            return TradeOutcome.LOSS
        return TradeOutcome.BREAKEVEN


# Re-exported so ``accounting`` remains the one import for "what did this
# trade do"; the statistics over a *set* of trades live in their own module.
from trading_bot.portfolio.statistics import (  # noqa: E402
    ReturnSample,
    TradeStatistics,
    max_drawdown,
    sample_returns,
    sharpe_ratio,
    sortino_ratio,
    summarise_trades,
    total_return_pct,
)

__all__ = [
    "FUNDING",
    "SPOT_BORROW",
    "FillLot",
    "PairedTrade",
    "PositionPnl",
    "ReturnSample",
    "TradeOutcome",
    "TradeStatistics",
    "Weighted",
    "max_drawdown",
    "position_pnl",
    "sample_returns",
    "sharpe_ratio",
    "sortino_ratio",
    "summarise_trades",
    "total_return_pct",
    "weigh",
]
