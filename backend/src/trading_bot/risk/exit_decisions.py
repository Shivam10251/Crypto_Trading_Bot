"""The two decisions a *close* produces, and why neither looks like an entry.

Extracted from ``risk.engine`` so that file stays the sequence of pre-trade
checks it is. The rules themselves are stated in
``trading_bot.risk.position_exit``; this module is where they are turned into
durable rows.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from trading_bot.core.logging import get_logger
from trading_bot.db.models.enums import ExecutionMode, RiskDecision, RiskEventType
from trading_bot.risk import position_exit
from trading_bot.risk.decisions import Denier
from trading_bot.risk.kill_switch import KillSwitchState
from trading_bot.risk.models import RiskEventDraft, RiskVerdict
from trading_bot.risk.store import RiskEventStore

logger = get_logger(__name__)


async def evaluate_exit(
    request: position_exit.ExitRequest,
    *,
    store: RiskEventStore,
    kill_switch: KillSwitchState,
    denier: Denier,
    mode: ExecutionMode,
    now: datetime,
) -> RiskVerdict:
    """Decide whether a close may be sent. A close is not an entry.

    Deliberately **not** gated on the kill switch: a kill stops new
    exposure, and refusing to reduce exposure while halted would leave
    the account holding exactly the risk the halt was called for. The
    switch's state is recorded on the decision instead, so a close made
    during a halt is visible as one. See
    ``trading_bot.risk.position_exit`` for why each rule differs from the
    entry path.

    What is enforced is reduce-only: a close may only give back exposure
    that exists, on the opposite side, up to what is still open. A
    violation is refused before any order is created.

    The decision must be durably stored before the caller may act on it.
    That is not the usual fail-closed trade-off - a database that cannot
    record the decision cannot record the close's own orders and fills
    either, and an unrecorded close is exposure the system believes it
    still holds.
    """
    context: dict[str, Any] = {
        "attempt_id": request.attempt_id,
        "exit_reason": request.reason,
        "legs": [leg.as_context() for leg in request.legs],
        # Annotation, never a gate: a close during a halt is allowed and
        # the row says it happened during one.
        "kill_switch_engaged": kill_switch.blocked_reason(),
        **(request.policy_context or {}),
    }
    violation = position_exit.check_reduce_only(request.legs, mode=mode)
    if violation is None and request.mode is not mode:
        violation = position_exit.ReduceOnlyViolation(
            None,
            f"close mode {request.mode.value} does not match risk engine mode {mode.value}",
        )
    if violation is not None:
        return await denier.rejected(
            RiskEventType.REDUCE_ONLY_VIOLATION,
            f"close refused as not strictly reduce-only: {violation.reason}",
            limit_name="reduce_only",
            context={**context, "violating_position_id": violation.position_id},
        )
    draft = RiskEventDraft(
        occurred_at=now,
        event_type=RiskEventType.POSITION_EXIT,
        decision=RiskDecision.APPROVED,
        mode=mode,
        intent_id=request.intent_id,
        reason=(
            f"closing attempt {request.attempt_id}: {request.reason}. "
            "Reduce-only and priced from the current books; this decision "
            "does not claim the close is profitable."
        ),
        is_shadow=False,
        strategy=request.strategy,
        limit_name=request.reason,
        context=context,
    )
    risk_event_id = await store.persist(draft)
    if risk_event_id is None:
        logger.error(
            "risk.exit_decision_not_durable",
            attempt_id=request.attempt_id,
            intent_id=request.intent_id,
        )
    return RiskVerdict(draft, risk_event_id)


async def record_residual(
    *,
    attempt_id: str,
    intent_id: str,
    strategy: str | None,
    residual: Mapping[str, Any],
    store: RiskEventStore,
    kill_switch: KillSwitchState,
    mode: ExecutionMode,
    now: datetime,
    pause_on_unhedged: bool,
) -> RiskVerdict:
    """A close that flattened one leg and left another carrying size.

    The same condition as an unhedged *entry*, arrived at from the other
    direction, and it gets the same response: a durable finding, and -
    under ``risk.pause_on_unhedged`` - the kill switch, so no new entry is
    taken while the book is carrying a leg with no hedge. The halt does
    not stop the residual from being closed: ``evaluate_exit`` is outside
    that gate on purpose.
    """
    should_pause = pause_on_unhedged
    reason = (
        f"closing attempt {attempt_id} left naked exposure: {residual}. "
        "The pair no longer hedges itself."
    )
    draft = RiskEventDraft(
        occurred_at=now,
        event_type=RiskEventType.ABNORMAL_EXECUTION,
        decision=RiskDecision.PAUSED if should_pause else RiskDecision.REJECTED,
        mode=mode,
        intent_id=f"{intent_id}:residual",
        reason=reason,
        is_shadow=False,
        strategy=strategy,
        limit_name="pause_on_unhedged",
        context={"attempt_id": attempt_id, "residual": dict(residual), "paused": should_pause},
    )
    risk_event_id = await store.persist(draft)
    if should_pause:
        await kill_switch.trigger(
            who="portfolio_closer",
            reason=reason,
            source="auto_pause_unhedged_close",
        )
    return RiskVerdict(draft, risk_event_id)
