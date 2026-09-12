"""Daily-loss and consecutive-loss controls, and what they do when they fire.

Pure policy: this module decides *whether* a control fires and what response
it calls for. ``RiskEngine`` owns the side effects (persisting the decision,
halting trading), so the rules stay testable without a database.

Three honest states these controls can be in, and they are not the same thing:

- **deferred** - there is no realised P&L to measure against (the portfolio
  service is not running, or its database cannot be read), so the control is
  not enforced. It is named in every approval's context so no row ever
  implies it passed.
- **incomplete** - a real ``PnlSource`` supplied a number and named cash
  flows it could not measure (funding, spot borrow). The limit is enforced
  against measured price P&L and fees, and every decision names that gap.
- **breached** - the measured figure is past the limit. A daily-loss breach
  halts trading for a configured period and then expires on its own; a
  consecutive-loss breach is a durable pause that needs review and an
  explicit re-arm, because "the strategy has stopped working" is not a
  condition a timer can clear.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from trading_bot.core.config import RiskConfig
from trading_bot.db.models.enums import RiskEventType
from trading_bot.risk.models import PnlSource
from trading_bot.risk.validation import LimitBreach


@dataclass(frozen=True, slots=True)
class LossLimitOutcome:
    """What the P&L-sourced controls concluded about this signal."""

    breach: LimitBreach | None = None
    #: Set when the response is a self-expiring halt rather than a durable one.
    halted_until: datetime | None = None
    #: True when clearing the halt needs a human and an explicit re-arm.
    requires_rearm: bool = False
    #: Controls that could not be evaluated at all, named for the audit row.
    deferred: tuple[str, ...] = ()
    #: Cash-flow components missing from the realised figure that *was*
    #: evaluated. Named so no approval implies the number was a total.
    incomplete: tuple[str, ...] = ()

    @property
    def halts_trading(self) -> bool:
        return self.breach is not None and (self.halted_until is not None or self.requires_rearm)


def _unavailable(limit_name: str, policy_name: str) -> LimitBreach:
    return LimitBreach(
        event_type=(
            RiskEventType.DAILY_LOSS_LIMIT
            if limit_name == "max_daily_loss_usd"
            else RiskEventType.CONSECUTIVE_LOSSES
        ),
        reason=(
            f"{limit_name} cannot be evaluated: no realised P&L is available "
            f"(the portfolio service is off, or its durable rows cannot be read). "
            f"Failing closed per the configured {policy_name}."
        ),
        limit_name=policy_name,
    )


async def evaluate(pnl: PnlSource, config: RiskConfig, now: datetime) -> LossLimitOutcome:
    """Evaluate both P&L-sourced controls. Never against a fabricated zero."""
    deferred: list[str] = []
    incomplete: list[str] = []

    daily = await pnl.realized_pnl_today_usd()
    if daily is None:
        if config.daily_loss_policy == "fail_closed":
            return LossLimitOutcome(
                breach=_unavailable("max_daily_loss_usd", "daily_loss_policy"),
                deferred=("max_daily_loss_usd",),
            )
        deferred.append("max_daily_loss_usd")
    else:
        incomplete.extend(daily.unmeasured)
        limit = Decimal(str(config.max_daily_loss_usd))
        if daily.net_usd <= -limit:
            missing = (
                f"; measured without {', '.join(daily.unmeasured)}" if daily.unmeasured else ""
            )
            return LossLimitOutcome(
                breach=LimitBreach(
                    RiskEventType.DAILY_LOSS_LIMIT,
                    f"realised loss since {daily.window_start.isoformat()} is "
                    f"{daily.net_usd} over {daily.trades} completed trades, past the "
                    f"-{limit} limit{missing}; trading is halted for "
                    f"{config.daily_loss_halt_minutes} minutes",
                    "max_daily_loss_usd",
                    limit,
                    daily.net_usd,
                ),
                halted_until=now + timedelta(minutes=config.daily_loss_halt_minutes),
                deferred=tuple(deferred),
                incomplete=tuple(incomplete),
            )
    losses = await pnl.consecutive_losses()
    if losses is None:
        if config.consecutive_loss_policy == "fail_closed":
            return LossLimitOutcome(
                breach=_unavailable("max_consecutive_losses", "consecutive_loss_policy"),
                deferred=tuple([*deferred, "max_consecutive_losses"]),
            )
        deferred.append("max_consecutive_losses")
    elif losses >= config.max_consecutive_losses:
        return LossLimitOutcome(
            breach=LimitBreach(
                RiskEventType.CONSECUTIVE_LOSSES,
                f"{losses} consecutive losses reaches the limit of "
                f"{config.max_consecutive_losses}; trading is paused pending review",
                "max_consecutive_losses",
                Decimal(config.max_consecutive_losses),
                Decimal(losses),
            ),
            requires_rearm=True,
            deferred=tuple(deferred),
            incomplete=tuple(incomplete),
        )

    return LossLimitOutcome(deferred=tuple(deferred), incomplete=tuple(incomplete))
