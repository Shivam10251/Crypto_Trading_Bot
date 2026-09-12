"""Persisting risk decisions - the one write an order is never allowed to skip.

Two write paths, because the durability requirement is different:

- ``persist`` is synchronous and awaited before a caller may act on its
  result. It is how ``APPROVED`` reaches the database *before* the order it
  authorises is ever submitted - the invariant this phase exists to hold.
- ``queue`` is best-effort, for events that gate nothing (queue-overload
  audit rows: nothing was ever going to be submitted for them). It follows
  the same bounded-queue-plus-periodic-flush shape as
  ``execution.recorder.ExecutionRecorder``, for the same reason: a database
  outage must not grow memory without bound or block a caller.

**Metrics only count committed rows, and a failed flush requeues the whole
batch.** Every row in one flush shares one transaction, so a failure at any
point - an ``execute`` mid-batch or the final ``commit`` - rolls back all of
them, not just the ones after the failure. Counting rows as written before
the commit returns, or requeueing only the tail, would silently lose risk
events and then report that it had not.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from trading_bot.core.logging import get_logger
from trading_bot.db.models import RiskEvent
from trading_bot.db.models.enums import ExecutionMode, RiskEventType
from trading_bot.risk.models import RiskEventDraft

logger = get_logger(__name__)

SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]

# Matches ExecutionRecorder/OpportunityRecorder: bounded so a database outage
# cannot grow this queue without limit.
MAX_PENDING_EVENTS = 2000
# All kill/re-arm writers take the same transaction-scoped PostgreSQL advisory
# lock before inserting. PostgreSQL sequence ids are allocated at INSERT time,
# not commit time; without serialization, ordering the audit rows by id does
# not tell us which concurrent state transition committed last.
KILL_SWITCH_ADVISORY_LOCK = 0x5249534B535749


class RiskEventStore:
    """Writes ``risk_events`` rows, synchronously when the caller needs proof."""

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory
        self._pending: list[RiskEventDraft] = []
        self.persisted = 0
        self.persist_failures = 0
        self.queued_written = 0
        self.queued_dropped = 0
        self.flush_failures = 0

    async def persist(self, draft: RiskEventDraft) -> int | None:
        """Insert and commit one row now, returning its id.

        ``None`` means the write could not be confirmed - a downed database,
        a lost connection, anything. The caller must fail closed: a decision
        that cannot be proven durable is not a decision an order may act on.
        Idempotent on ``(mode, intent_id, event_type)``: retrying the same
        evaluation converges on the row already written instead of adding one.

        The id is only returned once the session context manager has exited,
        which is where the commit happens - a commit that raises leaves this
        returning ``None`` rather than an id nobody can find later.
        """
        try:
            async with self._session_factory() as session:
                risk_event_id = await self._upsert(session, draft)
        except Exception as exc:
            self.persist_failures += 1
            logger.error(
                "risk.event_persist_failed",
                intent_id=draft.intent_id,
                event_type=draft.event_type.value,
                decision=draft.decision.value,
                error=str(exc),
            )
            return None
        self.persisted += 1
        return risk_event_id

    async def persist_kill_switch(self, draft: RiskEventDraft) -> int | None:
        """Persist one serialized kill-switch transition.

        Every process uses this path for trigger and re-arm. The advisory lock
        is held by the database transaction through commit, so the row ids of
        kill-switch transitions now reflect one serialized transition order.
        """
        try:
            async with self._session_factory() as session:
                await session.execute(
                    text("SELECT pg_advisory_xact_lock(:lock_id)"),
                    {"lock_id": KILL_SWITCH_ADVISORY_LOCK},
                )
                risk_event_id = await self._upsert(session, draft)
        except Exception as exc:
            self.persist_failures += 1
            logger.error(
                "risk.kill_switch_persist_failed",
                intent_id=draft.intent_id,
                decision=draft.decision.value,
                error=str(exc),
            )
            return None
        self.persisted += 1
        return risk_event_id

    async def _upsert(self, session: AsyncSession, draft: RiskEventDraft) -> int:
        """One upsert. Touches no metric: nothing here has committed yet."""
        row = draft.as_row()
        insert_statement = insert(RiskEvent).values(**row)
        updatable = {
            key: getattr(insert_statement.excluded, key)
            for key in row
            if key not in {"mode", "intent_id", "event_type"}
        }
        upsert_statement = insert_statement.on_conflict_do_update(
            constraint="mode_intent_event_type", set_=updatable
        ).returning(RiskEvent.id)
        result = await session.execute(upsert_statement)
        risk_event_id: int = result.scalar_one()
        return risk_event_id

    def queue(self, draft: RiskEventDraft) -> None:
        """Best-effort record: nothing downstream is waiting on this row."""
        if len(self._pending) >= MAX_PENDING_EVENTS:
            self.queued_dropped += 1
            self._pending.pop(0)
        self._pending.append(draft)

    @property
    def pending(self) -> int:
        return len(self._pending)

    async def flush(self) -> int:
        """Write every queued event in one transaction, or none of them.

        On any failure the *whole* batch goes back on the queue: they shared a
        transaction, so a failure anywhere rolled all of them back, including
        the ones whose ``execute`` had already returned.
        """
        if not self._pending:
            return 0
        batch, self._pending = self._pending, []
        try:
            async with self._session_factory() as session:
                for draft in batch:
                    await self._upsert(session, draft)
        except Exception as exc:
            # Oldest first, and bounded: a database that stays down must not
            # grow this queue without limit.
            self._pending = (batch + self._pending)[-MAX_PENDING_EVENTS:]
            self.flush_failures += 1
            logger.warning("risk.event_flush_failed", error=str(exc), requeued=len(batch))
            return 0
        self.queued_written += len(batch)
        return len(batch)

    async def run(self, *, interval_seconds: float) -> None:
        while True:
            await asyncio.sleep(interval_seconds)
            await self.flush()


async def latest_kill_switch_row(
    session_factory: SessionFactory, mode: ExecutionMode
) -> dict[str, Any] | None:
    """The most recent kill-switch decision, or ``None`` if none was ever made.

    Ordered by ``id``, never by ``occurred_at``. Kill-switch writers are
    serialized with ``KILL_SWITCH_ADVISORY_LOCK`` before their insert, so ids
    describe that serialized transition order without trusting process clocks.
    """
    async with session_factory() as session:
        result = await session.execute(
            select(RiskEvent)
            .where(RiskEvent.event_type == RiskEventType.KILL_SWITCH, RiskEvent.mode == mode)
            .order_by(RiskEvent.id.desc())
            .limit(1)
        )
        row = result.scalars().first()
        if row is None:
            return None
        return {
            "id": row.id,
            "decision": row.decision,
            "reason": row.reason,
            "context": row.context,
            "occurred_at": row.occurred_at,
        }
