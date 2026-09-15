"""Durable, audited kill-switch and pause state.

Current state is derived from the most recent ``risk_events`` row of type
``KILL_SWITCH`` rather than a separate table: the audit trail already answers
"are we halted, and why", and a second table could disagree with it after a
restart or a lost write.

**The in-process cache is a cache, not the truth.** It is loaded at startup
and then re-read on a bounded interval by ``run``, so a kill written by
another process - the ``trading-bot-risk`` CLI, or a second service - takes
effect in a running service within one poll interval instead of never.
``refresh`` is also what lets a timed halt expire and a re-arm land.

Fails closed in every direction that matters:

- if the database cannot be read at startup, the switch starts *active*
- if a re-arm cannot be durably recorded, the switch stays active
- if a trigger cannot be durably recorded, it still takes effect in-process,
  and a later ``refresh`` will not clear it just because the database has no
  record of it - an unpersisted halt is sticky until an explicit re-arm
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from trading_bot.core.logging import get_logger
from trading_bot.db.models.enums import ExecutionMode, RiskDecision, RiskEventType
from trading_bot.execution.account import AccountRejection
from trading_bot.execution.models import RejectionCode
from trading_bot.risk.models import RiskEventDraft, RiskVerdict
from trading_bot.risk.store import RiskEventStore, latest_kill_switch_row

logger = get_logger(__name__)

SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]

#: Called with the reason whenever the switch goes from clear to halted, so a
#: dispatcher can drop work it has accepted but not yet submitted. Any return
#: value is ignored - listeners report through their own counters and logs.
HaltListener = Callable[[str], object]


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _random_intent_id() -> str:
    return f"kill_switch:{uuid.uuid4().hex}"


def parse_halted_until(context: dict[str, object] | None) -> datetime | None:
    """A halt that declared its own expiry, e.g. a daily-loss cool-off."""
    if not context:
        return None
    raw = context.get("halted_until")
    if not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


class KillSwitchState:
    """The one flag that halts actionable execution, audited on every flip."""

    def __init__(
        self,
        store: RiskEventStore,
        session_factory: SessionFactory,
        *,
        mode: ExecutionMode = ExecutionMode.PAPER,
        clock: Callable[[], datetime] = _utcnow,
        backtest_run_id: int | None = None,
        intent_ids: Callable[[], str] = _random_intent_id,
    ) -> None:
        self._store = store
        self._session_factory = session_factory
        self._mode = mode
        self._clock = clock
        # A replayed run reads and writes only its own transitions, and names
        # them deterministically so a rerun produces the same audit rows.
        self._backtest_run_id = backtest_run_id
        self._intent_ids = intent_ids
        self._lock = asyncio.Lock()
        # Fail closed until ``load`` proves otherwise: a process that has not
        # yet checked durable state must not wave signals through on a guess.
        self._active = True
        self._reason = "risk state not yet loaded"
        self._halted_until: datetime | None = None
        self._loaded = False
        # The id of the durable decision this cache reflects. Compared on
        # refresh so an unchanged database costs nothing to re-apply.
        self._seen_id: int | None = None
        # A halt that took effect in-process but whose audit row never landed.
        # Refresh must not clear it: the database not knowing about a halt is
        # not evidence that trading is safe.
        self._unpersisted_halt = False
        self._listeners: list[HaltListener] = []
        self.refreshes = 0
        self.refresh_failures = 0
        self.listener_failures = 0

    # --- observation ----------------------------------------------------

    def add_listener(self, listener: HaltListener) -> None:
        """Register a callback for the clear -> halted transition."""
        self._listeners.append(listener)

    async def load(self) -> None:
        """Restore state from the audit trail. Call once, at startup."""
        async with self._lock:
            await self._read(initial=True)

    async def refresh(self) -> None:
        """Re-read durable state. Cheap, and safe to call on a timer."""
        # State application and local writes share one lock. In particular, a
        # poll must not clear a local halt while its audit write is in flight.
        async with self._lock:
            await self._read(initial=False)

    async def run(self, *, interval_seconds: float) -> None:
        """Poll durable state so another process's kill reaches this one."""
        while True:
            await asyncio.sleep(interval_seconds)
            await self.refresh()

    async def _read(self, *, initial: bool) -> None:
        try:
            row = await latest_kill_switch_row(
                self._session_factory, self._mode, self._backtest_run_id
            )
        except Exception as exc:
            self.refresh_failures += 1
            if initial:
                # Never seen durable state: assume the worst.
                self._halt("kill-switch state could not be loaded; failing closed", until=None)
                self._loaded = True
                logger.critical("risk.kill_switch_state_unknown", error=str(exc))
            else:
                # Already have a view; a transient read failure must not flip
                # it either way. Keeping the last known state is the only
                # answer that cannot invent one.
                logger.error("risk.kill_switch_refresh_failed", error=str(exc))
            return

        self.refreshes += 1
        self._loaded = True
        if row is None:
            if not self._unpersisted_halt:
                self._clear("no kill-switch decision on record")
            self._seen_id = None
            return
        if row["id"] == self._seen_id:
            return
        self._seen_id = int(row["id"])
        if row["decision"] is RiskDecision.PAUSED:
            self._halt(str(row["reason"]), until=parse_halted_until(row["context"]))
            self._unpersisted_halt = False
            logger.warning(
                "risk.kill_switch_observed_halt", reason=self._reason, risk_event_id=self._seen_id
            )
        elif not self._unpersisted_halt:
            self._clear(str(row["reason"]))
            logger.info("risk.kill_switch_observed_rearm", risk_event_id=self._seen_id)

    def _halt(self, reason: str, *, until: datetime | None) -> None:
        was_blocking = self.is_active
        self._active = True
        self._reason = reason
        self._halted_until = until
        if not was_blocking:
            self._notify(reason)

    def _clear(self, reason: str) -> None:
        self._active = False
        self._reason = reason
        self._halted_until = None

    def _notify(self, reason: str) -> None:
        for listener in self._listeners:
            try:
                listener(reason)
            except Exception as exc:  # a bad listener must not unhalt anything
                self.listener_failures += 1
                logger.exception("risk.kill_switch_listener_failed", error=str(exc))

    # --- reading --------------------------------------------------------

    @property
    def is_active(self) -> bool:
        return self.blocked_reason() is not None

    def blocked_reason(self) -> str | None:
        """Why actionable execution is halted right now, or ``None`` if it is not."""
        if not self._loaded:
            return "risk state not yet loaded; failing closed"
        if not self._active:
            return None
        if self._halted_until is not None and self._clock() >= self._halted_until:
            # A halt that declared its own window, and the window has passed.
            # It expires on its own terms rather than needing a re-arm, which
            # is what "disabled for the configured period" means.
            return None
        if self._halted_until is not None:
            return f"{self._reason} (until {self._halted_until.isoformat()})"
        return self._reason

    def guard(self) -> AccountRejection | None:
        """A ``PaperAccount.reserve`` guard: checked inside its own lock.

        Folding this into the account's atomic section is what prevents a
        kill-switch race - a signal approved a moment before the switch
        flips, and reserved a moment after, would otherwise slip through.
        """
        reason = self.blocked_reason()
        if reason is None:
            return None
        return AccountRejection(RejectionCode.RISK_PAUSED, reason)

    # --- writing --------------------------------------------------------

    async def trigger(
        self,
        *,
        who: str,
        reason: str,
        source: str = "manual",
        halted_until: datetime | None = None,
    ) -> RiskVerdict:
        """Halt actionable execution immediately, and record who/why.

        Takes effect in-process before the database write returns: stopping
        promptly is the safe direction, so a slow or failed audit write must
        not delay it. A failed write is logged critically, marks the halt
        sticky so a later refresh cannot clear it, and leaves the verdict's
        ``risk_event_id`` ``None`` so a caller can tell the difference.

        ``halted_until`` declares a self-expiring halt - the daily-loss
        cool-off - rather than one that needs an explicit re-arm.
        """
        async with self._lock:
            self._loaded = True
            self._halt(reason, until=halted_until)
            # Sticky before the first await: cancellation, a slow database, or
            # an unexpected failure must not create a window where a refresh
            # interprets the not-yet-visible row as permission to resume.
            self._unpersisted_halt = True
            context: dict[str, object] = {"action": "triggered", "who": who, "source": source}
            if halted_until is not None:
                context["halted_until"] = halted_until.isoformat()
            draft = RiskEventDraft(
                occurred_at=self._clock(),
                event_type=RiskEventType.KILL_SWITCH,
                decision=RiskDecision.PAUSED,
                mode=self._mode,
                intent_id=self._intent_ids(),
                reason=reason,
                context=context,
            )
            risk_event_id = await self._store.persist_kill_switch(draft)
            if risk_event_id is None:
                self._unpersisted_halt = True
                logger.critical("risk.kill_switch_trigger_not_durable", who=who, reason=reason)
            else:
                self._unpersisted_halt = False
                self._seen_id = risk_event_id
            return RiskVerdict(draft, risk_event_id)

    async def rearm(self, *, who: str, reason: str) -> RiskVerdict:
        """Resume actionable execution - only once the audit write succeeds.

        The opposite direction from ``trigger``: resuming is the risky move,
        so it stays halted until the database has confirmed who authorised it
        and why.
        """
        async with self._lock:
            draft = RiskEventDraft(
                occurred_at=self._clock(),
                event_type=RiskEventType.KILL_SWITCH,
                decision=RiskDecision.APPROVED,
                mode=self._mode,
                intent_id=self._intent_ids(),
                reason=reason,
                context={"action": "rearmed", "who": who},
            )
            risk_event_id = await self._store.persist_kill_switch(draft)
            if risk_event_id is None:
                logger.critical("risk.kill_switch_rearm_not_durable", who=who, reason=reason)
                return RiskVerdict(draft, None)
            self._clear("re-armed")
            self._unpersisted_halt = False
            self._loaded = True
            self._seen_id = risk_event_id
            logger.warning("risk.kill_switch_rearmed", who=who, reason=reason)
            return RiskVerdict(draft, risk_event_id)
