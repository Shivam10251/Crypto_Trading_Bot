"""When to stop holding a basis attempt. Pure policy; no I/O, no clock of its own.

A basis position has no natural end. The gross edge Phase 5 measures is a
*convergence* edge - what the trade is worth if the two mids meet - so
something has to decide when to stop waiting for that, and this module is
that decision and nothing else. Actually placing the orders is
``portfolio.closer``; valuing the exit is ``portfolio.valuation``.

## The basis, and its sign

A basis attempt buys one leg at ``P_buy`` and sells the other at ``P_sell``.
Ignoring fees, the pair's price P&L on quantity ``q`` is

```
q * [(P_sell_in - P_buy_in) - (P_sell_out - P_buy_out)]
```

so the pair profits when the spread it sold **narrows**. Both spreads are
expressed in basis points of the bought leg's entry price - one fixed
reference, so entry and exit are comparable - and then signed by the entry
direction:

```
entry_basis_bps  = (P_sell_in  - P_buy_in ) / P_buy_in * 10_000
exit_basis_bps   = (X_sell_out - X_buy_out) / P_buy_in * 10_000
direction        = +1 if entry_basis_bps >= 0 else -1
remaining_bps    = direction * exit_basis_bps      # basis still to converge
captured_bps     = direction * entry_basis_bps - remaining_bps
```

``X_*`` are **executable** prices from the current books - what buying the
sold leg back and selling the bought leg would actually cost - never mids.
``captured_bps`` is therefore what the pair has earned on price so far,
before fees, and it can be negative.

## Four reasons to close, in priority order

1. ``UNPAIRED_RESIDUAL`` - one leg is flat and another is not. The hedge does
   not exist; this is naked exposure and closing it is risk-reducing whatever
   the basis is doing.
2. ``ADVERSE_BASIS`` - the basis has widened against the entry by
   ``adverse_basis_bps``. A stop. It does not claim the exit is profitable,
   and it will usually not be.
3. ``MAX_HOLDING_PERIOD`` - held longer than ``max_holding_minutes``. A basis
   position held indefinitely stops being the trade that was made and becomes
   an unhedged bet on funding.
4. ``BASIS_CONVERGED`` - ``remaining_bps <= target_basis_bps``. The only
   reason that is a *target*, and the only one that requires a complete
   executable price on both legs: a partial fill at a worse price is not the
   convergence the target measured. The risk-reducing three are attempted
   even when depth is thin, because leaving the exposure on is worse.

Nothing here decides an exit is profitable. ``BASIS_CONVERGED`` says the
spread has come back, not that the round trip cleared its fees - measured in
Phase 8, the reachable taker floor is 30 bps, and a convergence inside that
is a loss the P&L will report as one.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from trading_bot.core.config import ExitPolicyConfig
from trading_bot.exchange.models import BPS_SCALE
from trading_bot.portfolio.valuation import ExecutableExit


class ExitReason(StrEnum):
    """Why a close was asked for. Stored on ``positions.exit_reason``."""

    UNPAIRED_RESIDUAL = "UNPAIRED_RESIDUAL"
    ADVERSE_BASIS = "ADVERSE_BASIS"
    MAX_HOLDING_PERIOD = "MAX_HOLDING_PERIOD"
    BASIS_CONVERGED = "BASIS_CONVERGED"


#: Reasons that reduce risk whatever the market is doing. These are attempted
#: on incomplete depth and are not blocked by the kill switch; the
#: convergence target is neither.
RISK_REDUCING = frozenset(
    {ExitReason.UNPAIRED_RESIDUAL, ExitReason.ADVERSE_BASIS, ExitReason.MAX_HOLDING_PERIOD}
)


class ExitDeferral(StrEnum):
    """Why an exit that a condition asked for was not attempted."""

    #: A leg's book could not price the exit at all.
    UNPRICEABLE = "UNPRICEABLE"
    #: Priced, but the visible depth could not fill the whole residual, and
    #: the reason asking for the exit was a target rather than a stop.
    INCOMPLETE_DEPTH = "INCOMPLETE_DEPTH"
    #: Every close attempt allowed by configuration has already been made.
    ATTEMPTS_EXHAUSTED = "ATTEMPTS_EXHAUSTED"


@dataclass(frozen=True, slots=True)
class BasisView:
    """One paired attempt as the policy needs to see it.

    Deliberately not the ORM row: the policy is pure, so it is testable
    against arithmetic rather than against a database.
    """

    attempt_id: str
    opened_at: datetime
    #: Entry fill prices, weighted. ``None`` for a leg that is already flat.
    buy_entry_price: Decimal | None
    sell_entry_price: Decimal | None
    #: What flattening each live leg would fetch, from the current books.
    buy_exit: ExecutableExit | None
    sell_exit: ExecutableExit | None
    is_unpaired: bool
    close_attempts: int = 0

    @property
    def has_both_legs(self) -> bool:
        return self.buy_entry_price is not None and self.sell_entry_price is not None


@dataclass(frozen=True, slots=True)
class ExitDecision:
    """Close, or do not, and the arithmetic behind either answer."""

    reason: ExitReason | None
    detail: str
    deferral: ExitDeferral | None = None
    entry_basis_bps: Decimal | None = None
    exit_basis_bps: Decimal | None = None
    remaining_bps: Decimal | None = None
    captured_bps: Decimal | None = None
    holding: timedelta | None = None

    @property
    def should_close(self) -> bool:
        return self.reason is not None and self.deferral is None

    def context(self) -> dict[str, object]:
        """The audit row's ``context``: every number behind this decision."""
        return {
            "exit_reason": self.reason.value if self.reason is not None else None,
            "deferral": self.deferral.value if self.deferral is not None else None,
            "detail": self.detail,
            "entry_basis_bps": _maybe_str(self.entry_basis_bps),
            "exit_basis_bps": _maybe_str(self.exit_basis_bps),
            "remaining_bps": _maybe_str(self.remaining_bps),
            "captured_bps": _maybe_str(self.captured_bps),
            "holding_seconds": (
                int(self.holding.total_seconds()) if self.holding is not None else None
            ),
        }


def _maybe_str(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def basis_bps(sell_price: Decimal, buy_price: Decimal, reference: Decimal) -> Decimal:
    """The sold leg over the bought leg, in bps of one fixed reference price."""
    if reference <= 0:
        raise ValueError("basis reference price must be positive")
    return (sell_price - buy_price) / reference * BPS_SCALE


def evaluate(view: BasisView, config: ExitPolicyConfig, now: datetime) -> ExitDecision:
    """Decide whether this attempt should be closed now, and say why."""
    holding = now - view.opened_at
    exhausted = view.close_attempts >= config.max_close_attempts

    if view.is_unpaired:
        return _closing(
            ExitReason.UNPAIRED_RESIDUAL,
            "one leg is flat and the other is not: residual exposure is unhedged",
            holding=holding,
            exhausted=exhausted,
        )

    over_time = holding >= timedelta(minutes=config.max_holding_minutes)
    if not view.has_both_legs or view.buy_exit is None or view.sell_exit is None:
        # No basis to measure. A position we cannot price still has to obey
        # the holding limit - that is the condition that needs no price.
        if over_time:
            return _closing(
                ExitReason.MAX_HOLDING_PERIOD,
                f"held {holding}, past the {config.max_holding_minutes} minute limit; "
                "the basis could not be priced",
                holding=holding,
                exhausted=exhausted,
            )
        return ExitDecision(
            reason=None,
            detail="the current books cannot price this attempt's exit",
            deferral=ExitDeferral.UNPRICEABLE,
            holding=holding,
        )

    assert view.buy_entry_price is not None and view.sell_entry_price is not None
    reference = view.buy_entry_price
    entry = basis_bps(view.sell_entry_price, view.buy_entry_price, reference)
    direction = Decimal(1) if entry >= 0 else Decimal(-1)

    if view.buy_exit.price is None or view.sell_exit.price is None:
        if over_time:
            return _closing(
                ExitReason.MAX_HOLDING_PERIOD,
                f"held {holding}, past the {config.max_holding_minutes} minute limit; "
                f"exit unpriceable ({view.buy_exit.problem}/{view.sell_exit.problem})",
                holding=holding,
                entry_basis_bps=entry,
                exhausted=exhausted,
            )
        return ExitDecision(
            reason=None,
            detail=(
                f"exit unpriceable: buy leg {view.buy_exit.problem}, "
                f"sell leg {view.sell_exit.problem}"
            ),
            deferral=ExitDeferral.UNPRICEABLE,
            entry_basis_bps=entry,
            holding=holding,
        )

    # The sold leg is bought back and the bought leg is sold, so the exit
    # spread is priced from the sides that would actually have to trade.
    exit_basis = basis_bps(view.sell_exit.price, view.buy_exit.price, reference)
    remaining = direction * exit_basis
    captured = direction * entry - remaining
    complete_depth = view.buy_exit.complete and view.sell_exit.complete
    numbers = {
        "entry_basis_bps": entry,
        "exit_basis_bps": exit_basis,
        "remaining_bps": remaining,
        "captured_bps": captured,
        "holding": holding,
    }

    widened = remaining - direction * entry
    if widened >= Decimal(str(config.adverse_basis_bps)):
        return _closing(
            ExitReason.ADVERSE_BASIS,
            f"basis widened {widened.quantize(Decimal('0.01'))} bps against the entry, "
            f"past the {config.adverse_basis_bps} bps stop",
            exhausted=exhausted,
            **numbers,  # type: ignore[arg-type]
        )
    if over_time:
        return _closing(
            ExitReason.MAX_HOLDING_PERIOD,
            f"held {holding}, past the {config.max_holding_minutes} minute limit",
            exhausted=exhausted,
            **numbers,  # type: ignore[arg-type]
        )
    if remaining <= Decimal(str(config.target_basis_bps)):
        if not complete_depth:
            # A convergence target priced against depth that cannot fill it is
            # not the trade the target described. Risk-reducing exits take
            # what depth there is; this one waits.
            return ExitDecision(
                reason=ExitReason.BASIS_CONVERGED,
                detail=(
                    "basis converged but the books cannot fill the whole residual: "
                    f"buy leg {view.buy_exit.fillable}/{view.buy_exit.quantity}, "
                    f"sell leg {view.sell_exit.fillable}/{view.sell_exit.quantity}"
                ),
                deferral=ExitDeferral.INCOMPLETE_DEPTH,
                **numbers,  # type: ignore[arg-type]
            )
        return _closing(
            ExitReason.BASIS_CONVERGED,
            f"basis converged to {remaining.quantize(Decimal('0.01'))} bps, "
            f"within the {config.target_basis_bps} bps target; this says the spread "
            "came back, not that the round trip cleared its fees",
            exhausted=exhausted,
            **numbers,  # type: ignore[arg-type]
        )
    return ExitDecision(
        reason=None,
        detail=f"{remaining.quantize(Decimal('0.01'))} bps of basis still to converge",
        **numbers,  # type: ignore[arg-type]
    )


def _closing(
    reason: ExitReason,
    detail: str,
    *,
    exhausted: bool,
    entry_basis_bps: Decimal | None = None,
    exit_basis_bps: Decimal | None = None,
    remaining_bps: Decimal | None = None,
    captured_bps: Decimal | None = None,
    holding: timedelta | None = None,
) -> ExitDecision:
    """A condition fired. Deferred only when its retries are exhausted."""
    return ExitDecision(
        reason=reason,
        detail=detail,
        deferral=ExitDeferral.ATTEMPTS_EXHAUSTED if exhausted else None,
        entry_basis_bps=entry_basis_bps,
        exit_basis_bps=exit_basis_bps,
        remaining_bps=remaining_bps,
        captured_bps=captured_bps,
        holding=holding,
    )
