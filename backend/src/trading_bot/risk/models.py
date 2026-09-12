"""The risk engine's vocabulary: verdicts, drafts, and the P&L it cannot see yet.

Strategy-independent on purpose: a buggy or over-confident strategy must not
be able to talk its way past a limit, so nothing here imports the strategy
layer beyond the plain domain types (``Signal``) it has to read.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Protocol

from trading_bot.db.models.enums import ExecutionMode, RiskDecision, RiskEventType


@dataclass(frozen=True, slots=True)
class RiskEventDraft:
    """Everything one ``risk_events`` row needs, before it has an id."""

    occurred_at: datetime
    event_type: RiskEventType
    decision: RiskDecision
    mode: ExecutionMode
    intent_id: str
    reason: str
    is_shadow: bool = False
    opportunity_uid: uuid.UUID | None = None
    strategy: str | None = None
    limit_name: str | None = None
    limit_value: Decimal | None = None
    observed_value: Decimal | None = None
    context: Mapping[str, Any] | None = None

    def as_row(self) -> dict[str, Any]:
        return {
            "occurred_at": self.occurred_at,
            "event_type": self.event_type,
            "decision": self.decision,
            "mode": self.mode,
            "intent_id": self.intent_id,
            "opportunity_uid": self.opportunity_uid,
            "is_shadow": self.is_shadow,
            "strategy": self.strategy,
            "limit_name": self.limit_name,
            "limit_value": self.limit_value,
            "observed_value": self.observed_value,
            "reason": self.reason,
            "context": dict(self.context) if self.context is not None else None,
        }


@dataclass(frozen=True, slots=True)
class RiskVerdict:
    """One decision, and the durable row it produced (when it produced one).

    ``risk_event_id`` is ``None`` only when persistence itself failed - the
    caller must treat that the same as ``REJECTED``: an order must never be
    submitted on the strength of a decision nobody can prove was made.
    """

    draft: RiskEventDraft
    risk_event_id: int | None

    @property
    def decision(self) -> RiskDecision:
        return self.draft.decision

    @property
    def is_approved(self) -> bool:
        return self.decision is RiskDecision.APPROVED and self.risk_event_id is not None

    @property
    def reason(self) -> str:
        return self.draft.reason


class PnlSource(Protocol):
    """Realised P&L, when something can supply it.

    Phase 10 owns the real implementation. Until it exists, every caller gets
    ``None`` rather than a fabricated zero - a losing day and an unmeasured
    one must never look the same to a limit that is supposed to stop trading.
    """

    def realized_pnl_today_usd(self) -> Decimal | None: ...

    def consecutive_losses(self) -> int | None: ...


@dataclass(frozen=True, slots=True)
class NullPnlSource:
    """The honest default: no realized P&L exists before Phase 10."""

    def realized_pnl_today_usd(self) -> Decimal | None:
        return None

    def consecutive_losses(self) -> int | None:
        return None


class PausePolicy(StrEnum):
    """Which configured policy decides whether a finding halts trading.

    Naming the policy on the finding keeps "what happened" separate from
    "what we do about it": the review always reports the finding, and
    configuration alone decides whether it also stops trading.
    """

    #: Recorded for research, never halts on its own.
    NONE = "NONE"
    #: Naked exposure - governed by ``risk.pause_on_unhedged``.
    UNHEDGED = "UNHEDGED"
    #: Execution behaving unlike the approval: realised slippage or latency
    #: past their limits, or an adapter failure/timeout. Governed by
    #: ``risk.pause_on_abnormal_execution``.
    ABNORMAL = "ABNORMAL"


@dataclass(slots=True)
class PostTradeFinding:
    """One thing the post-trade review noticed, ranked by how much it matters."""

    event_type: RiskEventType
    reason: str
    limit_name: str | None = None
    limit_value: Decimal | None = None
    observed_value: Decimal | None = None
    pause_policy: PausePolicy = PausePolicy.NONE
