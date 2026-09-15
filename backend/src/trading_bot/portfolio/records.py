"""What a position and a basis attempt look like once their fills are attached.

Read models, not ORM rows: the accounting is pure, so the layer that computes
it is handed plain values rather than a session-bound object it could
accidentally lazy-load from.

The unit that matters is the **attempt**, not the leg. A basis trade's spot
leg losing what its perpetual leg made is the intended outcome, so a win or a
loss is decided on ``AttemptRecord``, and a leg on its own is never scored.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from trading_bot.db.models.enums import ExecutionMode, MarketType, PositionStatus, Side
from trading_bot.exchange.models import MarketRef
from trading_bot.portfolio.accounting import FillLot, PairedTrade, PositionPnl, position_pnl


@dataclass(frozen=True, slots=True)
class LegRecord:
    """One position row plus the fills that made it, ready to be accounted."""

    position_id: int
    market_id: int
    ref: MarketRef
    market_type: MarketType
    mode: ExecutionMode
    strategy: str
    attempt_id: str
    side: Side
    status: PositionStatus
    quantity: Decimal
    closed_quantity: Decimal
    entry_price: Decimal
    opened_at: datetime
    closed_at: datetime | None
    close_intent_id: str | None
    close_claimed_at: datetime | None
    close_attempts: int
    entries: tuple[FillLot, ...]
    exits: tuple[FillLot, ...]
    #: Signed funding attributed to a flat perpetual leg from recorded
    #: settlements (Phase 11 replay). ``None`` - every paper leg today - means
    #: unmeasured, never zero.
    funding_pnl_usd: Decimal | None = None

    @property
    def open_quantity(self) -> Decimal:
        return self.quantity - self.closed_quantity

    @property
    def borrows(self) -> bool:
        """A short spot leg is held on borrowed inventory and accrues a cost."""
        return self.market_type is MarketType.SPOT and self.side is Side.SELL

    def accounting(self, *, mark_price: Decimal | None = None) -> PositionPnl:
        return position_pnl(
            side=self.side,
            entries=self.entries,
            exits=self.exits,
            mark_price=mark_price,
            # Funding does not apply to spot. A perpetual leg carries it only
            # when its settlements were attributed; otherwise it is named as
            # missing rather than treated as zero.
            funding_pnl_usd=(
                Decimal(0) if self.market_type is MarketType.SPOT else self.funding_pnl_usd
            ),
            borrow_cost_usd=None,
            borrows=self.borrows,
        )


@dataclass(frozen=True, slots=True)
class AttemptRecord:
    """Both legs of one basis attempt - the unit a win or loss is decided on."""

    attempt_id: str
    mode: ExecutionMode
    strategy: str
    legs: tuple[LegRecord, ...]

    @property
    def live_legs(self) -> tuple[LegRecord, ...]:
        return tuple(leg for leg in self.legs if leg.open_quantity > 0)

    @property
    def buy_leg(self) -> LegRecord | None:
        return next((leg for leg in self.legs if leg.side is Side.BUY), None)

    @property
    def sell_leg(self) -> LegRecord | None:
        return next((leg for leg in self.legs if leg.side is Side.SELL), None)

    @property
    def is_claimed(self) -> bool:
        return any(leg.status is PositionStatus.CLOSING for leg in self.legs)

    @property
    def is_unpaired(self) -> bool:
        """The attempt does not currently carry a complete, balanced hedge.

        Read from the durable ``closed_quantity`` rather than from the fills,
        so it agrees with the exposure ``live_legs`` reports. The pure
        ``PairedTrade.is_unpaired`` answers the same question from fills, for
        callers that have no rows.
        """
        live = self.live_legs
        if not live:
            return False
        if len(self.legs) != 2 or len(live) != 2:
            return True
        return live[0].open_quantity != live[1].open_quantity

    @property
    def close_attempts(self) -> int:
        return max((leg.close_attempts for leg in self.legs), default=0)

    def paired(self, marks: dict[int, Decimal | None] | None = None) -> PairedTrade:
        marks = marks or {}
        legs = tuple(leg.accounting(mark_price=marks.get(leg.position_id)) for leg in self.legs)
        opened = [leg.opened_at for leg in self.legs]
        closed = [leg.closed_at for leg in self.legs]
        return PairedTrade(
            attempt_id=self.attempt_id,
            strategy=self.strategy,
            legs=legs,
            opened_at=min(opened) if opened else None,
            closed_at=max(closed) if closed and all(at is not None for at in closed) else None,  # type: ignore[type-var]
        )


@dataclass(frozen=True, slots=True)
class CloseClaim:
    """The rows actually claimed, not the possibly stale caller's view."""

    intent_id: str
    claim_id: str
    sequence: int
    attempt: AttemptRecord


def close_intent_id(attempt_id: str, sequence: int) -> str:
    """Deterministic identity for one close of one attempt.

    Replaying the same claim produces the same order ids and converges on its
    rows. A genuinely new claim increments the sequence, so a terminal failed
    order is never overwritten or resubmitted under the same identity.
    """
    return f"close:{attempt_id}:{sequence}"


def close_client_order_id(intent_id: str, leg: int) -> str:
    return f"{intent_id}-{leg}"
