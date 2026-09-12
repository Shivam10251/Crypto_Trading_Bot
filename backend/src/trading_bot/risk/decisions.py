"""Writing one risk refusal, and naming what it refused.

Extracted from ``risk.engine`` so the engine file reads as the sequence of
checks it is, rather than as checks interleaved with row-building. Nothing
here decides anything: ``Denier`` turns an already-made decision into a
durable ``risk_events`` row, and the helpers below put the right names on it.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import datetime
from decimal import Decimal
from typing import Any

from trading_bot.core.logging import get_logger
from trading_bot.db.models.enums import ExecutionMode, RiskDecision, RiskEventType
from trading_bot.execution.account import AccountRejection
from trading_bot.execution.models import RejectionCode
from trading_bot.risk.models import RiskEventDraft, RiskVerdict
from trading_bot.risk.store import RiskEventStore
from trading_bot.risk.validation import LimitBreach
from trading_bot.strategy.models import Signal

logger = get_logger(__name__)


class Denier:
    """Builds and persists one refusal, so each call site stays one line."""

    __slots__ = (
        "_clock",
        "_intent_id",
        "_is_shadow",
        "_mode",
        "_opportunity_uid",
        "_store",
        "_strategy",
    )

    def __init__(
        self,
        store: RiskEventStore,
        clock: Callable[[], datetime],
        mode: ExecutionMode,
        intent_id: str,
        opportunity_uid: uuid.UUID | None,
        is_shadow: bool,
        strategy: str | None,
    ) -> None:
        self._store = store
        self._clock = clock
        self._mode = mode
        self._intent_id = intent_id
        self._opportunity_uid = opportunity_uid
        self._is_shadow = is_shadow
        self._strategy = strategy

    async def rejected(
        self,
        event_type: RiskEventType,
        reason: str,
        *,
        limit_name: str | None = None,
        limit_value: Decimal | None = None,
        observed_value: Decimal | None = None,
        context: dict[str, Any] | None = None,
    ) -> RiskVerdict:
        return await self._write(
            RiskDecision.REJECTED,
            event_type,
            reason,
            limit_name,
            limit_value,
            observed_value,
            context,
        )

    async def paused(
        self,
        event_type: RiskEventType,
        reason: str,
        *,
        limit_name: str | None = None,
        limit_value: Decimal | None = None,
        observed_value: Decimal | None = None,
        context: dict[str, Any] | None = None,
    ) -> RiskVerdict:
        return await self._write(
            RiskDecision.PAUSED,
            event_type,
            reason,
            limit_name,
            limit_value,
            observed_value,
            context,
        )

    async def breach(
        self, breach: LimitBreach, *, context: dict[str, Any] | None = None
    ) -> RiskVerdict:
        return await self.rejected(
            breach.event_type,
            breach.reason,
            limit_name=breach.limit_name,
            limit_value=breach.limit_value,
            observed_value=breach.observed_value,
            context=context,
        )

    async def from_account(
        self, decision: RiskDecision, event_type: RiskEventType, rejection: AccountRejection
    ) -> RiskVerdict:
        return await self._write(
            decision,
            event_type,
            rejection.detail,
            rejection.limit_name,
            rejection.limit_value,
            rejection.observed_value,
            None,
        )

    async def _write(
        self,
        decision: RiskDecision,
        event_type: RiskEventType,
        reason: str,
        limit_name: str | None,
        limit_value: Decimal | None,
        observed_value: Decimal | None,
        context: dict[str, Any] | None,
    ) -> RiskVerdict:
        draft = RiskEventDraft(
            occurred_at=self._clock(),
            event_type=event_type,
            decision=decision,
            mode=self._mode,
            intent_id=self._intent_id,
            reason=reason,
            is_shadow=self._is_shadow,
            opportunity_uid=self._opportunity_uid,
            strategy=self._strategy,
            limit_name=limit_name,
            limit_value=limit_value,
            observed_value=observed_value,
            context=context,
        )
        risk_event_id = await self._store.persist(draft)
        if risk_event_id is None:
            logger.error(
                "risk.decision_not_durable",
                intent_id=self._intent_id,
                event_type=event_type.value,
                decision=decision.value,
            )
        return RiskVerdict(draft, risk_event_id)


def expected_slippage_bps(signal: Signal) -> Decimal:
    """Adverse slippage summed across both legs.

    ``Leg.slippage_bps`` is already floored at zero per leg, so a leg priced
    better than the mid cannot net off one priced worse - the same rule the
    post-trade measurement applies to realised fills.
    """
    return sum((leg.slippage_bps for leg in signal.legs), Decimal(0))


def enforced_controls(deferred: tuple[str, ...]) -> list[str]:
    controls = [
        "max_order_notional_usd",
        "max_position_notional_usd",
        "max_total_exposure_usd",
        "max_slippage_bps",
        "max_latency_ms",
        "max_stale_data_ms",
        "max_funding_age_ms",
        "max_daily_loss_usd",
        "max_consecutive_losses",
    ]
    return [name for name in controls if name not in deferred]


def approval_reason(deferred: tuple[str, ...]) -> str:
    """Never claim every limit passed when some were never evaluated."""
    if not deferred:
        return "within every configured limit"
    return f"within every enforced limit; not evaluated: {', '.join(deferred)}"


def map_account_rejection(rejection: AccountRejection) -> tuple[RiskEventType, RiskDecision]:
    """Translate an execution-layer rejection into the risk vocabulary.

    Uses the rejection's own structured ``limit_name`` rather than parsing
    ``detail`` text, so a wording change in ``PaperAccount`` cannot silently
    misclassify a decision.
    """
    if rejection.code is RejectionCode.RISK_PAUSED:
        return RiskEventType.KILL_SWITCH, RiskDecision.PAUSED
    if rejection.code is RejectionCode.EXPOSURE_LIMIT:
        if rejection.limit_name == "max_order_notional_usd":
            return RiskEventType.ORDER_SIZE_EXCEEDED, RiskDecision.REJECTED
        if rejection.limit_name == "max_position_notional_usd":
            return RiskEventType.POSITION_LIMIT_EXCEEDED, RiskDecision.REJECTED
        return RiskEventType.EXPOSURE_LIMIT_EXCEEDED, RiskDecision.REJECTED
    return RiskEventType.INSUFFICIENT_RESOURCES, RiskDecision.REJECTED
