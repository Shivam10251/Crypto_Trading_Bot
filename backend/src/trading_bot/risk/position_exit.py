"""Risk rules for closing a position, which are not the rules for opening one.

An entry and an exit are opposite actions and it would be a mistake to gate
them with one set of checks. Three differences, each deliberate:

**Reduce-only, enforced here rather than trusted.** A close may only give back
exposure that exists: the opposite side, a positive quantity, and no more than
the position still has open. Anything else - a close on the same side, a
quantity past the open size, a position in another mode or a shadow probe -
is a ``REDUCE_ONLY_VIOLATION`` and no order is created for it. This is checked
even though ``portfolio.closer`` computes the quantity itself, because "the
caller worked it out correctly" is not a safety property.

**The kill switch does not block a close.** A kill stops *new* exposure. A
close removes exposure, so refusing it while halted would leave the account
holding the very risk the halt was called for - and Phase 9's own
documentation says a kill "does not close positions" precisely because
nothing existed to close them safely. The switch's state is recorded on the
decision, so a close made during a halt is visible as one.

**No capacity is reserved.** ``PaperAccount.reserve`` exists to stop an entry
consuming more than the account has. A close consumes nothing; it releases.
The account is updated after the fills are durable, by ``settle_exit``.

What a close *is* still gated on: the decision has to be durably recorded
before any order is sent. That is not the fail-closed direction it looks
like - the same database that cannot store the decision cannot store the
close's orders and fills either, and an unrecorded close is exposure the
system believes it still has. Refusing to act on a database it cannot write
to is the only answer that keeps the record and the account agreeing.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from trading_bot.db.models.enums import ExecutionMode, PositionStatus, Side
from trading_bot.exchange.models import MarketRef


@dataclass(frozen=True, slots=True)
class ExitLeg:
    """One leg of a close, as risk needs to see it."""

    position_id: int
    ref: MarketRef
    #: The side the position was **entered** on.
    entry_side: Side
    #: The side the close would trade - must be the opposite.
    close_side: Side
    quantity: Decimal
    open_quantity: Decimal
    status: PositionStatus
    mode: ExecutionMode = ExecutionMode.PAPER
    is_shadow: bool = False

    def as_context(self) -> dict[str, object]:
        return {
            "position_id": self.position_id,
            "market": str(self.ref),
            "entry_side": self.entry_side.value,
            "close_side": self.close_side.value,
            "quantity": str(self.quantity),
            "open_quantity": str(self.open_quantity),
            "mode": self.mode.value,
        }


@dataclass(frozen=True, slots=True)
class ExitRequest:
    """A close of one basis attempt, both legs together."""

    attempt_id: str
    intent_id: str
    strategy: str
    reason: str
    legs: tuple[ExitLeg, ...]
    #: The exit policy's own arithmetic, carried onto the audit row.
    policy_context: dict[str, object] | None = None
    mode: ExecutionMode = ExecutionMode.PAPER

    @property
    def notional_hint(self) -> Decimal:
        return sum((leg.quantity for leg in self.legs), Decimal(0))


@dataclass(frozen=True, slots=True)
class ReduceOnlyViolation:
    """Why a close was refused before any order existed."""

    position_id: int | None
    reason: str


def check_reduce_only(
    legs: Sequence[ExitLeg], *, mode: ExecutionMode | None = None
) -> ReduceOnlyViolation | None:
    """Every way a "close" could fail to be one. ``None`` admits it."""
    if not legs:
        return ReduceOnlyViolation(None, "a close must name at least one position")
    for leg in legs:
        if mode is not None and leg.mode is not mode:
            return ReduceOnlyViolation(
                leg.position_id,
                f"position mode {leg.mode.value} does not match close mode {mode.value}",
            )
        if leg.is_shadow:
            return ReduceOnlyViolation(
                leg.position_id,
                "a shadow probe's exposure is hypothetical and must never be closed",
            )
        if leg.status is PositionStatus.CLOSED:
            return ReduceOnlyViolation(
                leg.position_id, "position is already closed; there is nothing to reduce"
            )
        if leg.status is PositionStatus.LIQUIDATED:
            return ReduceOnlyViolation(
                leg.position_id, "position was liquidated; there is nothing left to close"
            )
        expected = Side.SELL if leg.entry_side is Side.BUY else Side.BUY
        if leg.close_side is not expected:
            return ReduceOnlyViolation(
                leg.position_id,
                f"closing a {leg.entry_side.value} position needs a {expected.value}, "
                f"not another {leg.close_side.value}: that would increase it",
            )
        if leg.quantity <= 0:
            return ReduceOnlyViolation(
                leg.position_id, f"close quantity {leg.quantity} is not positive"
            )
        if leg.open_quantity <= 0:
            return ReduceOnlyViolation(
                leg.position_id, "position has no open quantity left to close"
            )
        if leg.quantity > leg.open_quantity:
            return ReduceOnlyViolation(
                leg.position_id,
                f"close quantity {leg.quantity} exceeds the {leg.open_quantity} still open: "
                "that would reverse the position rather than reduce it",
            )
    return None
