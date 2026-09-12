"""The risk engine: the one gate every signal crosses before an order can exist.

```
Signal -> RiskEngine.evaluate -> APPROVED -> RiskEngine.admit -> execution
                               -> REJECTED -> durable risk event, no order
                               -> PAUSED   -> durable state, no order
```

Independent of the strategy on purpose (see ``docs/risk-management.md``): every
check re-derives its own answer from the opportunity's evidence and the
account's own ledger, rather than trusting whatever the strategy already
decided - including the ages the strategy recorded, which are recomputed from
the underlying timestamps against the current clock.

**Three checkpoints, not one.** Conditions that can change between deciding
and submitting are checked at evaluation, again once the approval is durably
stored, and once more in ``admit`` - which the coordinator calls immediately
before it hands anything to an adapter. The kill switch is folded into the
account's own reservation lock at each of those points via
``KillSwitchState.guard``, so a switch that flips concurrently cannot slip an
order past a check that had already passed.

**Shadow probes never touch actionable state.** They reserve against a
separate ``PaperAccount``, so a probe can neither consume nor be blocked by
the capacity a real signal is competing for.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from trading_bot.core.config import RiskConfig
from trading_bot.core.logging import get_logger
from trading_bot.db.models.enums import ExecutionMode, RiskDecision, RiskEventType
from trading_bot.execution.account import AccountRejection, AccountReservation, PaperAccount
from trading_bot.execution.coordinator import ExecutionAttempt
from trading_bot.execution.models import RejectionCode
from trading_bot.risk import decisions, exit_decisions, loss_limits, position_exit, post_trade
from trading_bot.risk.kill_switch import KillSwitchState
from trading_bot.risk.models import NullPnlSource, PnlSource, RiskEventDraft, RiskVerdict
from trading_bot.risk.store import RiskEventStore
from trading_bot.risk.validation import (
    LimitBreach,
    check_temporal,
    decision_latency_ms,
    validate_evidence,
    validate_expiry_timestamp,
)
from trading_bot.strategy.models import Signal

logger = get_logger(__name__)


def _utcnow() -> datetime:
    return datetime.now(UTC)


class RiskEngine:
    """Strategy-independent pre-trade, admission and post-trade decisions."""

    def __init__(
        self,
        config: RiskConfig,
        account: PaperAccount,
        store: RiskEventStore,
        kill_switch: KillSwitchState,
        *,
        shadow_account: PaperAccount | None = None,
        pnl_source: PnlSource | None = None,
        mode: ExecutionMode = ExecutionMode.PAPER,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._config = config
        self._account = account
        # Shadow probes get their own ledger. Sharing one would let a probe
        # consume, delay or be rejected by the capacity a real signal needs -
        # research contaminating trading, in either direction.
        self._shadow_account = shadow_account
        self._store = store
        self._kill_switch = kill_switch
        self._pnl_source = pnl_source or NullPnlSource()
        self._mode = mode
        self._clock = clock

    def _denier(
        self,
        intent_id: str,
        opportunity_uid: uuid.UUID | None,
        is_shadow: bool,
        strategy: str | None,
    ) -> decisions.Denier:
        return decisions.Denier(
            self._store, self._clock, self._mode, intent_id, opportunity_uid, is_shadow, strategy
        )

    # --- pre-trade ------------------------------------------------------

    async def evaluate(
        self,
        signal: Signal,
        *,
        intent_id: str,
        opportunity_uid: uuid.UUID | None,
        is_shadow: bool,
    ) -> RiskVerdict:
        now = self._clock()
        strategy = signal.strategy
        deny = self._denier(intent_id, opportunity_uid, is_shadow, strategy)

        # Shadow probes are isolated from actionable state: the kill switch,
        # the P&L-sourced controls and the account's real exposure must never
        # gate - or be gated by - research-only probes.
        if not is_shadow:
            blocked = self._kill_switch.blocked_reason()
            if blocked is not None:
                return await deny.paused(RiskEventType.KILL_SWITCH, blocked)

        account = self._account if not is_shadow else self._shadow_account
        if account is None:
            return await deny.rejected(
                RiskEventType.FAIL_CLOSED,
                "no isolated shadow account is configured; refusing to reserve "
                "a probe against actionable capacity",
            )

        invalid_expiry = validate_expiry_timestamp(signal)
        if invalid_expiry is not None:
            return await deny.breach(invalid_expiry)

        if signal.is_expired(now):
            return await deny.rejected(
                RiskEventType.SIGNAL_EXPIRED,
                f"signal expired at {signal.expires_at.isoformat()}, now {now.isoformat()}",
                limit_name="expires_at",
            )

        breach = validate_evidence(signal, now) or check_temporal(
            signal,
            now,
            max_stale_data_ms=self._config.max_stale_data_ms,
            max_funding_age_ms=self._config.max_funding_age_ms,
            max_latency_ms=self._config.max_latency_ms,
        )
        if breach is not None:
            return await deny.breach(breach)

        slippage_breach = self._check_slippage(signal)
        if slippage_breach is not None:
            return await deny.breach(slippage_breach)

        deferred: tuple[str, ...] = ()
        incomplete: tuple[str, ...] = ()
        if not is_shadow:
            outcome = await loss_limits.evaluate(self._pnl_source, self._config, now)
            deferred = outcome.deferred
            incomplete = outcome.incomplete
            if outcome.breach is not None:
                return await self._deny_loss_limit(outcome, deny)

        guard = self._kill_switch.guard if not is_shadow else None
        reserved = await account.reserve(signal, intent_id, guard=guard)
        if isinstance(reserved, AccountRejection):
            event_type, decision = decisions.map_account_rejection(reserved)
            return await deny.from_account(decision, event_type, reserved)

        draft = RiskEventDraft(
            occurred_at=now,
            event_type=RiskEventType.PRE_TRADE_CHECK,
            decision=RiskDecision.APPROVED,
            mode=self._mode,
            intent_id=intent_id,
            reason=decisions.approval_reason(deferred),
            is_shadow=is_shadow,
            opportunity_uid=opportunity_uid,
            strategy=strategy,
            context={
                "decision_latency_ms": decision_latency_ms(signal, now),
                "expected_slippage_bps": str(decisions.expected_slippage_bps(signal)),
                "gross_exposure_usd": str(account.gross_exposure_usd),
                "notional_usd": str(signal.opportunity.notional_usd),
                # Never let an approval imply a control that was not evaluated
                # had passed. These are named, every time, on every row.
                "deferred_controls": list(deferred),
                "enforced": decisions.enforced_controls(deferred),
                # ...nor imply the realised P&L a control *did* use was a
                # total when cash flows are missing from it.
                "incomplete_pnl_components": list(incomplete),
            },
        )
        risk_event_id = await self._store.persist(draft)
        if risk_event_id is None:
            await account.release(reserved)
            return await deny.rejected(
                RiskEventType.FAIL_CLOSED,
                "the approval could not be durably recorded; failing closed",
            )

        # The approval is durable, but time passed while it was written and the
        # switch may have flipped during that write. Re-check before handing
        # anything back to the caller; ``admit`` checks once more at the very
        # last moment before submission.
        withdrawal = await self._withdraw_if_changed(
            signal, account, reserved, deny, risk_event_id, is_shadow
        )
        return withdrawal or RiskVerdict(draft, risk_event_id)

    async def _withdraw_if_changed(
        self,
        signal: Signal,
        account: PaperAccount,
        reservation: AccountReservation,
        deny: decisions.Denier,
        approval_id: int,
        is_shadow: bool,
    ) -> RiskVerdict | None:
        """Undo a just-granted approval if its preconditions no longer hold."""
        now = self._clock()
        blocked = None if is_shadow else self._kill_switch.blocked_reason()
        breach = check_temporal(
            signal,
            now,
            max_stale_data_ms=self._config.max_stale_data_ms,
            max_funding_age_ms=self._config.max_funding_age_ms,
            max_latency_ms=self._config.max_latency_ms,
        )
        if blocked is None and breach is None:
            return None
        await account.release(reservation)
        context = {"withdrew_approval_risk_event_id": approval_id}
        if blocked is not None:
            return await deny.paused(
                RiskEventType.KILL_SWITCH,
                f"halted while the approval was being recorded: {blocked}",
                context=context,
            )
        assert breach is not None
        return await deny.breach(breach, context=context)

    async def _deny_loss_limit(
        self, outcome: loss_limits.LossLimitOutcome, deny: decisions.Denier
    ) -> RiskVerdict:
        """A P&L control fired: halt trading the way its policy describes."""
        breach = outcome.breach
        assert breach is not None
        if outcome.halts_trading:
            await self._kill_switch.trigger(
                who="risk_engine",
                reason=breach.reason,
                source=breach.limit_name,
                halted_until=outcome.halted_until,
            )
            return await deny.paused(
                breach.event_type,
                breach.reason,
                limit_name=breach.limit_name,
                limit_value=breach.limit_value,
                observed_value=breach.observed_value,
                context={"requires_rearm": outcome.requires_rearm},
            )
        return await deny.breach(breach)

    def _check_slippage(self, signal: Signal) -> LimitBreach | None:
        """Aggregate adverse slippage, the same definition used post-trade."""
        expected = decisions.expected_slippage_bps(signal)
        limit = Decimal(str(self._config.max_slippage_bps))
        if expected <= limit:
            return None
        return LimitBreach(
            RiskEventType.SLIPPAGE_EXCEEDED,
            f"expected slippage across both legs is {expected} bps, over the {limit} bps limit",
            "max_slippage_bps",
            limit,
            expected,
        )

    # --- admission ------------------------------------------------------

    async def admit(
        self,
        signal: Signal,
        *,
        intent_id: str,
        opportunity_uid: uuid.UUID | None,
        is_shadow: bool,
    ) -> AccountRejection | None:
        """The last check before an adapter sees the order. ``None`` admits it.

        Called by ``ExecutionCoordinator`` immediately before submission, so
        the window between "this was approved" and "this was sent" is as
        close to zero as the code can make it. A refusal here releases the
        reservation and produces no order at all - the durable risk event
        written here is the whole record of it.
        """
        now = self._clock()
        deny = self._denier(intent_id, opportunity_uid, is_shadow, signal.strategy)
        if not is_shadow:
            blocked = self._kill_switch.blocked_reason()
            if blocked is not None:
                await deny.paused(RiskEventType.KILL_SWITCH, f"halted before submission: {blocked}")
                return AccountRejection(RejectionCode.RISK_PAUSED, blocked)
        invalid_expiry = validate_expiry_timestamp(signal)
        if invalid_expiry is not None:
            await deny.breach(invalid_expiry, context={"withdrawn_at": "admission"})
            return AccountRejection(RejectionCode.RISK_WITHDRAWN, invalid_expiry.reason)
        if signal.is_expired(now):
            reason = f"signal expired at {signal.expires_at.isoformat()} before submission"
            await deny.rejected(RiskEventType.SIGNAL_EXPIRED, reason, limit_name="expires_at")
            return AccountRejection(RejectionCode.SIGNAL_EXPIRED, reason)
        breach = check_temporal(
            signal,
            now,
            max_stale_data_ms=self._config.max_stale_data_ms,
            max_funding_age_ms=self._config.max_funding_age_ms,
            max_latency_ms=self._config.max_latency_ms,
        )
        if breach is not None:
            await deny.breach(breach, context={"withdrawn_at": "admission"})
            return AccountRejection(
                RejectionCode.RISK_WITHDRAWN, f"{breach.limit_name}: {breach.reason}"
            )
        return None

    # --- exits ----------------------------------------------------------

    async def evaluate_exit(self, request: position_exit.ExitRequest) -> RiskVerdict:
        """Decide whether a close may be sent. See ``risk.exit_decisions``."""
        return await exit_decisions.evaluate_exit(
            request,
            store=self._store,
            kill_switch=self._kill_switch,
            denier=self._denier(request.intent_id, None, False, request.strategy),
            mode=self._mode,
            now=self._clock(),
        )

    async def record_exit_residual(
        self,
        *,
        attempt_id: str,
        intent_id: str,
        strategy: str | None,
        residual: Mapping[str, Any],
    ) -> RiskVerdict:
        """A close that left a leg naked. See ``risk.exit_decisions``."""
        return await exit_decisions.record_residual(
            attempt_id=attempt_id,
            intent_id=intent_id,
            strategy=strategy,
            residual=residual,
            store=self._store,
            kill_switch=self._kill_switch,
            mode=self._mode,
            now=self._clock(),
            pause_on_unhedged=self._config.pause_on_unhedged,
        )

    # --- queue ----------------------------------------------------------

    def halted_reason(self) -> str | None:
        """Why actionable work is not being accepted, or ``None`` if it is.

        Read by the dispatcher so halted work is refused at the door rather
        than queued for a worker to reject a moment later.
        """
        return self._kill_switch.blocked_reason()

    def record_queue_overload(
        self,
        *,
        intent_id: str,
        opportunity_uid: uuid.UUID | None,
        is_shadow: bool,
        strategy: str | None,
        queue_size: int,
    ) -> None:
        """Best-effort audit of a signal the dispatcher could not even queue.

        Nothing was ever going to be submitted for this intent - the item
        never entered the queue - so unlike ``evaluate``'s durable write,
        losing this one costs the research record a row, not a safety
        invariant. Queued for the next periodic flush rather than awaited.
        """
        self._store.queue(
            RiskEventDraft(
                occurred_at=self._clock(),
                event_type=RiskEventType.QUEUE_OVERLOAD,
                decision=RiskDecision.REJECTED,
                mode=self._mode,
                intent_id=intent_id,
                reason=f"execution queue was full at its configured bound of {queue_size}",
                is_shadow=is_shadow,
                opportunity_uid=opportunity_uid,
                strategy=strategy,
                limit_name="execution.queue_size",
                limit_value=Decimal(queue_size),
                observed_value=Decimal(queue_size),
            )
        )

    def record_discarded(
        self,
        *,
        intent_id: str,
        opportunity_uid: uuid.UUID | None,
        is_shadow: bool,
        strategy: str | None,
        reason: str,
    ) -> None:
        """Audit work dropped from the queue because trading was halted."""
        self._store.queue(
            RiskEventDraft(
                occurred_at=self._clock(),
                event_type=RiskEventType.KILL_SWITCH,
                decision=RiskDecision.PAUSED,
                mode=self._mode,
                intent_id=intent_id,
                reason=reason,
                is_shadow=is_shadow,
                opportunity_uid=opportunity_uid,
                strategy=strategy,
                limit_name="kill_switch",
            )
        )

    # --- post-trade -----------------------------------------------------

    async def evaluate_post_trade(
        self,
        attempt: ExecutionAttempt,
        signal: Signal,
        *,
        intent_id: str,
        opportunity_uid: uuid.UUID | None,
    ) -> RiskVerdict | None:
        """Review what an attempt actually did. ``None`` when nothing stands out.

        Shadow probes are excluded entirely: their exposure is hypothetical by
        design and must never mutate pause state or the actionable kill
        switch. A fully hedged, on-time, on-cost attempt produces no row -
        that is already fully described by its own order and fill rows, and
        ``risk_events`` records *decisions*, not routine confirmations.
        """
        if attempt.is_shadow:
            return None
        findings = post_trade.findings_for(attempt, self._config)
        findings.extend(
            post_trade.account_limit_finding(breach)
            for breach in await self._account.current_limit_breaches()
        )
        if not findings:
            return None
        chosen = post_trade.primary(findings)
        should_pause = post_trade.should_pause(findings, self._config)
        decision = RiskDecision.PAUSED if should_pause else RiskDecision.REJECTED
        draft = RiskEventDraft(
            occurred_at=self._clock(),
            event_type=chosen.event_type,
            decision=decision,
            mode=self._mode,
            intent_id=intent_id,
            reason=chosen.reason,
            is_shadow=False,
            opportunity_uid=opportunity_uid,
            strategy=signal.strategy,
            limit_name=chosen.limit_name,
            limit_value=chosen.limit_value,
            observed_value=chosen.observed_value,
            context={
                "attempt_id": attempt.attempt_id,
                "findings": [post_trade.serialise(f) for f in findings],
                "paused": should_pause,
            },
        )
        risk_event_id = await self._store.persist(draft)
        if should_pause:
            await self._kill_switch.trigger(
                who="risk_engine",
                reason=(
                    f"execution outside its limits on attempt {attempt.attempt_id}: "
                    f"{chosen.reason} (risk_event_id={risk_event_id})"
                ),
                source="post_trade_review",
            )
        return RiskVerdict(draft, risk_event_id)
