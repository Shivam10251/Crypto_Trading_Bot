"""The durable life of a backtest run: created, running, and how it ended.

Lifecycle timestamps here are **wall-clock** - they describe the process that
ran the replay, not the replayed market. Everything the replay produced is
stamped in virtual time instead.

A run is not resumable. Its paper account, open episodes and replay state live
in memory, so a process that dies mid-run leaves a row that would claim to be
RUNNING forever; ``recover_orphans`` marks any such row FAILED once its
heartbeat is older than the configured bound, and every CLI command runs it
first. The heartbeat is written by its own wall-clock task, independent of
replay progress, so a run that is slow - a long drain, a large final flush -
is still heart-beating and is not mistaken for a dead one.

Every transition is a conditional write on the current status, so the races
between a cancel, a start, a heartbeat, orphan recovery and completion each
resolve to exactly one terminal state:

| Race | Outcome |
| --- | --- |
| cancel before ``mark_running`` | CANCELLED; the replay never starts |
| cancel while RUNNING | CANCELLED at the next tick boundary |
| cancel after the replay reached its end | the finished status; request kept |
| orphan recovery marks a slow run FAILED | FAILED; the run stops (``LOST``) |
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import uuid
from collections.abc import Callable, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from trading_bot.core.config import Settings
from trading_bot.db.models import BacktestRun
from trading_bot.db.models.enums import TERMINAL_BACKTEST_STATUSES, BacktestRunStatus
from trading_bot.exchange.models import MarketRef

SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]

#: Configuration sections a replay reads. Database, API, logging and
#: exchange credentials are deliberately absent: they cannot change a result,
#: and secrets must never be copied into a research table.
SNAPSHOT_SECTIONS = (
    "markets",
    "market_data",
    "monitoring",
    "strategy",
    "opportunities",
    "costs",
    "risk",
    "portfolio",
    "execution",
    "backtest",
)
_REPOSITORY = Path(__file__).resolve().parents[4]


def config_snapshot(settings: Settings) -> dict[str, Any]:
    snapshot: dict[str, Any] = {
        section: getattr(settings, section).model_dump(mode="json") for section in SNAPSHOT_SECTIONS
    }
    snapshot["exchange"] = {"venue": settings.exchange.venue}
    # Never part of a replay's behaviour, and never stored.
    snapshot["execution"].pop("live_confirmation_phrase", None)
    return snapshot


def config_hash(snapshot: dict[str, Any]) -> str:
    canonical = json.dumps(snapshot, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def code_revision(repository: Path = _REPOSITORY) -> tuple[str | None, bool | None, str | None]:
    """``(HEAD, dirty, source digest)``; unknown values are returned as NULL.

    The digest includes HEAD, the tracked binary diff and every untracked,
    non-ignored path and its bytes. Only the hash is stored, never source or
    secrets. It distinguishes two dirty trees that share the same commit.
    """
    try:
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],  # noqa: S607 - git on PATH
            cwd=repository,
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        ).stdout.strip()
        tracked_status = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=no"],  # noqa: S607
            cwd=repository,
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        ).stdout
        diff = subprocess.run(
            ["git", "diff", "--binary", "HEAD", "--"],  # noqa: S607
            cwd=repository,
            capture_output=True,
            check=True,
            timeout=15,
        ).stdout
        untracked = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard", "-z"],  # noqa: S607
            cwd=repository,
            capture_output=True,
            check=True,
            timeout=5,
        ).stdout.split(b"\0")
    except (OSError, subprocess.SubprocessError):
        return None, None, None
    digest = hashlib.sha256()
    digest.update(head.encode())
    digest.update(b"\0tracked\0")
    digest.update(diff)
    try:
        source_untracked = sorted(path for path in untracked if path and _source_untracked(path))
        for raw_path in source_untracked:
            digest.update(b"\0untracked\0" + raw_path + b"\0")
            digest.update((repository / raw_path.decode()).read_bytes())
    except OSError:
        return head or None, bool(tracked_status.strip()) or bool(source_untracked), None
    return (
        (head or None),
        bool(tracked_status.strip()) or bool(source_untracked),
        digest.hexdigest(),
    )


def _source_untracked(raw_path: bytes) -> bool:
    """Whether an untracked path should influence replay code provenance."""
    path = raw_path.decode()
    parts = path.split("/")
    return ".claude-flow" not in parts


@dataclass(frozen=True, slots=True)
class RunIdentity:
    id: int
    run_uid: uuid.UUID
    config_hash: str


@dataclass(slots=True)
class RunProgress:
    """Counters written with every heartbeat and at the end.

    Event counters, precisely:

    - ``initialization_events``: observations received before the start that
      were in force at it (one per market and stream at most).
    - ``events_accepted`` / ``events_rejected``: every in-range row of the
      *whole requested range*, as the validator judged it - including rows
      after the point a cancelled or failed run stopped at, once drained.
    - ``events_replayed``: accepted events actually applied to the replayed
      market state before the run ended. Equal to ``events_accepted`` for a
      run that reached its end.
    """

    initialization_events: int = 0
    events_accepted: int = 0
    events_rejected: int = 0
    events_replayed: int = 0
    evaluations: int = 0
    opportunities_recorded: int = 0
    orders_recorded: int = 0
    fills_recorded: int = 0
    trades_completed: int = 0

    def values(self) -> dict[str, int]:
        return {
            "initialization_events": self.initialization_events,
            "events_accepted": self.events_accepted,
            "events_rejected": self.events_rejected,
            "events_replayed": self.events_replayed,
            "evaluations": self.evaluations,
            "opportunities_recorded": self.opportunities_recorded,
            "orders_recorded": self.orders_recorded,
            "fills_recorded": self.fills_recorded,
            "trades_completed": self.trades_completed,
        }


class Heartbeat(Enum):
    RUNNING = "running"
    #: Someone asked the run to stop; it stops at its next tick boundary.
    CANCEL_REQUESTED = "cancel_requested"
    #: The row is no longer RUNNING - another process finished it (orphan
    #: recovery, most likely). This process must stop and must not overwrite.
    LOST = "lost"


def _utcnow() -> datetime:
    return datetime.now(UTC)


class RunStore:
    def __init__(
        self, session_factory: SessionFactory, *, wall_clock: Callable[[], datetime] = _utcnow
    ) -> None:
        self._session_factory = session_factory
        self._wall_clock = wall_clock

    async def create(
        self,
        *,
        source: str,
        start: datetime,
        end: datetime,
        refs: Sequence[MarketRef],
        snapshot: dict[str, Any],
        revision: tuple[str | None, bool | None, str | None],
    ) -> RunIdentity:
        digest = config_hash(snapshot)
        run = BacktestRun(
            run_uid=uuid.uuid4(),
            status=BacktestRunStatus.PENDING,
            dataset_source=source,
            requested_start=start,
            requested_end=end,
            markets=sorted(str(ref) for ref in refs),
            config_snapshot=snapshot,
            config_hash=digest,
            code_revision=revision[0],
            code_dirty=revision[1],
            code_worktree_hash=revision[2],
        )
        async with self._session_factory() as session:
            session.add(run)
            await session.flush()
            return RunIdentity(id=run.id, run_uid=run.run_uid, config_hash=digest)

    async def mark_running(self, run_id: int) -> bool:
        """PENDING -> RUNNING, only if nobody cancelled it first.

        ``False`` means the run is no longer PENDING - a cancel landed between
        creation and start - and must not be replayed.
        """
        now = self._wall_clock()
        async with self._session_factory() as session:
            result = await session.execute(
                update(BacktestRun)
                .where(
                    BacktestRun.id == run_id,
                    BacktestRun.status == BacktestRunStatus.PENDING,
                    BacktestRun.cancel_requested_at.is_(None),
                )
                .values(status=BacktestRunStatus.RUNNING, started_at=now, heartbeat_at=now)
                .returning(BacktestRun.id)
            )
            return result.first() is not None

    async def heartbeat(self, run_id: int, progress: RunProgress) -> Heartbeat:
        """Record liveness and counts, and learn whether to keep going."""
        async with self._session_factory() as session:
            result = await session.execute(
                update(BacktestRun)
                .where(BacktestRun.id == run_id, BacktestRun.status == BacktestRunStatus.RUNNING)
                .values(heartbeat_at=self._wall_clock(), **progress.values())
                .returning(BacktestRun.cancel_requested_at)
            )
            row = result.first()
        if row is None:
            return Heartbeat.LOST
        return Heartbeat.CANCEL_REQUESTED if row[0] is not None else Heartbeat.RUNNING

    async def finish(
        self,
        run_id: int,
        status: BacktestRunStatus,
        *,
        progress: RunProgress,
        actual_start: datetime | None,
        actual_end: datetime | None,
        fingerprint: str | None,
        warnings: Sequence[str],
        dataset_issues: dict[str, Any] | None,
        completeness: dict[str, Any] | None,
        failure_reason: str | None = None,
    ) -> BacktestRunStatus:
        """Write the terminal state once; returns the status actually stored.

        Terminal transitions are idempotent and never overwrite each other. A
        row already terminal - cancelled while PENDING, or failed by orphan
        recovery while this process was slow - keeps its status, and that
        status is returned for the caller to report. A cancel *request* that
        arrives after the replay reached its end does not turn a finished run
        into a cancelled one: the run completed, and ``cancel_requested_at``
        stays as evidence that someone asked.
        """
        if status not in TERMINAL_BACKTEST_STATUSES:
            raise ValueError(f"{status.value} is not a terminal status")
        now = self._wall_clock()
        async with self._session_factory() as session:
            run = await session.get(BacktestRun, run_id, with_for_update=True)
            if run is None:
                raise LookupError(f"backtest run {run_id} does not exist")
            if run.status in TERMINAL_BACKTEST_STATUSES:
                return run.status
            run.status = status
            run.completed_at = now
            run.heartbeat_at = now
            run.started_at = run.started_at or now
            run.actual_start = actual_start
            run.actual_end = actual_end
            run.dataset_fingerprint = fingerprint
            run.warnings = list(warnings) or None
            run.dataset_issues = dataset_issues
            run.completeness = completeness
            run.failure_reason = failure_reason
            for key, value in progress.values().items():
                setattr(run, key, value)
            return status

    async def request_cancel(self, run_uid: uuid.UUID) -> BacktestRun | None:
        """Ask a running or pending run to stop. Returns the row as it now is.

        The row is locked first, so a start racing this request either sees
        the cancel (and does not start) or has already started (and sees the
        request on its next heartbeat) - never a RUNNING row marked CANCELLED.
        """
        async with self._session_factory() as session:
            run = await self._by_uid(session, run_uid, lock=True)
            if run is None or run.status in TERMINAL_BACKTEST_STATUSES:
                return run
            if run.cancel_requested_at is None:
                run.cancel_requested_at = self._wall_clock()
            if run.status is BacktestRunStatus.PENDING:
                # Nothing is running it, so nothing else would ever finish it.
                run.status = BacktestRunStatus.CANCELLED
                run.started_at = run.started_at or run.cancel_requested_at
                run.completed_at = run.cancel_requested_at
            await session.flush()
            return run

    async def get(self, run_uid: uuid.UUID) -> BacktestRun | None:
        async with self._session_factory() as session:
            return await self._by_uid(session, run_uid)

    async def recent(self, limit: int = 20) -> list[BacktestRun]:
        async with self._session_factory() as session:
            rows = await session.execute(
                select(BacktestRun).order_by(BacktestRun.id.desc()).limit(limit)
            )
            return list(rows.scalars())

    async def recover_orphans(self, *, stale_after: timedelta) -> list[uuid.UUID]:
        """Mark RUNNING rows whose process stopped heart-beating as FAILED."""
        now = self._wall_clock()
        cutoff = now - stale_after
        async with self._session_factory() as session:
            result = await session.execute(
                update(BacktestRun)
                .where(
                    BacktestRun.status == BacktestRunStatus.RUNNING,
                    BacktestRun.heartbeat_at < cutoff,
                )
                .values(
                    status=BacktestRunStatus.FAILED,
                    completed_at=now,
                    failure_reason=(
                        "interrupted: no heartbeat since the process running it stopped; "
                        "a backtest cannot resume, rerun it"
                    ),
                )
                .returning(BacktestRun.run_uid)
            )
            return [row[0] for row in result]

    @staticmethod
    async def _by_uid(
        session: AsyncSession, run_uid: uuid.UUID, *, lock: bool = False
    ) -> BacktestRun | None:
        statement = select(BacktestRun).where(BacktestRun.run_uid == run_uid)
        if lock:
            statement = statement.with_for_update()
        result = await session.execute(statement)
        return result.scalars().first()
