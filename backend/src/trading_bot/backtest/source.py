"""The historical-data boundary, and what is wrong with a dataset.

``HistoricalDataSource`` is deliberately narrow: open one consistent view of
the data, describe what it holds for a request, hand over the state that was
already in force at the start, and stream the range in bounded, ordered
batches. Everything a replay needs to judge the data - duplicates,
regressions, gaps, corrupt rows, impossible timestamps - is checked here once,
source-independently, by ``EventValidator``, so a second source (files,
another database) inherits the same rules instead of re-implementing them.

Nothing here repairs data. A duplicate or a regression is dropped and counted,
a corrupt row is skipped and counted, and a gap is recorded with its span.
Whether a gap made a market unusable is not decided here: the replayed book
ages through it, and the strategy, the simulator and valuation already refuse
a stale book.

**The dataset fingerprint** is a SHA-256 over the complete validated input of
a run, in replay order: the market reference data, every initialization
observation, every event of the *whole requested range* that was accepted,
and a marker for every row that was rejected and why. It does not depend on
how far the replay got or how batches were cut, so two runs over the same
source state have the same fingerprint, and any change to a field a
component could observe changes it.
"""

from __future__ import annotations

import hashlib
from collections import Counter
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field, fields
from datetime import datetime, timedelta
from typing import Any, Protocol

from trading_bot.backtest.events import EventKind, ReplayEvent
from trading_bot.exchange.models import FundingInfo, MarketRef, MarketSpec, OrderBook, Quote

#: Samples kept per issue kind; counts are always complete.
MAX_ISSUE_SAMPLES = 20
#: Funding intervals a venue can actually run: whole hours that divide a day.
VALID_FUNDING_INTERVALS = frozenset(hours for hours in range(1, 25) if 24 % hours == 0)
#: Issue kinds that reject an event for an impossible or corrupt timestamp.
TEMPORAL_ISSUES = (
    "naive_timestamp",
    "future_exchange_timestamp",
    "implausible_exchange_lag",
    "invalid_funding_schedule",
    "invalid_funding_interval",
)


@dataclass(frozen=True, slots=True)
class DatasetRequest:
    """``[start, end)`` of receipt time, for these markets."""

    start: datetime
    end: datetime
    refs: tuple[MarketRef, ...]
    batch_size: int = 5_000

    def __post_init__(self) -> None:
        if self.start.tzinfo is None or self.end.tzinfo is None:
            raise ValueError("a dataset request needs timezone-aware bounds")
        if self.end <= self.start:
            raise ValueError("a dataset request needs end > start")
        if self.batch_size < 1:
            raise ValueError("batch_size must be positive")
        if not self.refs:
            raise ValueError("a dataset request needs at least one market")


@dataclass(frozen=True, slots=True)
class MarketCoverage:
    ref: MarketRef
    quotes: int
    books: int
    funding: int
    first_at: datetime | None
    last_at: datetime | None


@dataclass(frozen=True, slots=True)
class DatasetCoverage:
    """What a source holds for a request, before anything is replayed."""

    markets: tuple[MarketCoverage, ...]
    #: Requested markets the source has no reference data for at all.
    unknown: tuple[MarketRef, ...]
    specs: dict[MarketRef, MarketSpec]
    #: Reference-data fields the source could not reconstruct, e.g. filters
    #: that were never persisted. The simulator cannot check what is missing.
    unsupported_filters: tuple[str, ...] = ()
    #: When each market's stored reference data last changed - part of the
    #: fingerprint, because a filter refresh changes what an order must pass.
    spec_versions: dict[MarketRef, str] = field(default_factory=dict)
    #: What the source guarantees about consistency, e.g. the snapshot id.
    consistency: str | None = None
    #: Windows the recorder itself reported losing inside the range.
    capture_gaps: tuple[str, ...] = ()

    @property
    def events(self) -> int:
        return sum(market.quotes + market.books + market.funding for market in self.markets)


@dataclass(slots=True)
class ReplayBatch:
    events: list[ReplayEvent]
    #: Rows the source could not turn into a valid event, as (kind, detail).
    corrupt: list[tuple[str, str]] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class Lookback:
    """How far before the start each kind of state may still be in force."""

    quote: timedelta
    book: timedelta
    funding: timedelta

    def for_kind(self, kind: EventKind) -> timedelta:
        return {EventKind.QUOTE: self.quote, EventKind.BOOK: self.book}.get(kind, self.funding)


class HistoricalDataSource(Protocol):
    """One consistent view of recorded history.

    Between ``open`` and ``close`` every call must read the same source state:
    rows written or deleted meanwhile are invisible, so a run's inputs are a
    property of the dataset and not of when its pages happened to be read.
    """

    name: str

    @property
    def market_ids(self) -> dict[MarketRef, int]:
        """``markets.id`` per ref - replayed rows reference the real instruments."""
        ...

    async def open(self) -> None: ...

    async def close(self) -> None: ...

    async def coverage(self, request: DatasetRequest) -> DatasetCoverage: ...

    async def initial_state(self, request: DatasetRequest, lookback: Lookback) -> ReplayBatch:
        """For each market and kind, the newest rows received before the start,
        within ``lookback``, newest first: the validator picks the first valid one."""
        ...

    def stream(self, request: DatasetRequest) -> AsyncIterator[ReplayBatch]:
        """Events in ``order_key`` order, at most ``batch_size`` per batch."""
        ...


@dataclass(slots=True)
class DatasetIssues:
    """Counts of everything wrong with the data, and bounded samples of each."""

    counts: Counter[str] = field(default_factory=Counter)
    samples: dict[str, list[str]] = field(default_factory=dict)
    #: Longest gap seen per market and stream kind, in milliseconds.
    longest_gap_ms: dict[str, int] = field(default_factory=dict)

    def add(self, kind: str, detail: str) -> None:
        self.counts[kind] += 1
        bucket = self.samples.setdefault(kind, [])
        if len(bucket) < MAX_ISSUE_SAMPLES:
            bucket.append(detail)

    def as_dict(self) -> dict[str, Any]:
        return {
            "counts": dict(sorted(self.counts.items())),
            "samples": {kind: list(values) for kind, values in sorted(self.samples.items())},
            "longest_gap_ms": dict(sorted(self.longest_gap_ms.items())),
        }


@dataclass(frozen=True, slots=True)
class TemporalLimits:
    #: How far a venue clock may run ahead of ours before a row is corrupt.
    max_clock_skew_ms: int = 1_000
    #: How far behind ours a venue clock may be before a row is corrupt.
    max_exchange_lag_ms: int = 300_000
    #: Slack around a funding observation's settlement schedule.
    funding_schedule_tolerance_ms: int = 60_000


@dataclass(slots=True)
class _StreamState:
    last_at: datetime
    last_sequence: int | None
    last_funding_time: datetime | None


def digest_specs(
    digest: Any, specs: Mapping[MarketRef, MarketSpec], versions: Mapping[MarketRef, str]
) -> None:
    """Every stored reference-data field, per market, in a stable order."""
    for ref in sorted(specs, key=str):
        spec = specs[ref]
        parts = [
            "spec",
            str(ref),
            versions.get(ref),
            *(f"{item.name}={getattr(spec, item.name)}" for item in fields(spec)),
        ]
        digest.update(("\x1f".join(str(part) for part in parts) + "\x1e").encode())


class EventValidator:
    """Decides, per event, whether it may be applied - and records why not.

    - **duplicate**: the same venue sequence as the last accepted event of
      that market and kind, or - with no sequence - the same receipt time.
    - **regression**: a lower sequence than already applied, or a funding
      observation whose next settlement is earlier than one already seen.
    - **gap**: more than ``gap_ms`` of receipt time between consecutive
      events of one market and kind (``funding_gap_ms`` for funding, which is
      polled on a slower cadence by design), including from the start of the
      request (or the initialization observation) to the first event and from
      the last event to its end.
    - **out_of_order**: an event earlier than one already applied - a source
      that broke its ordering contract. It is refused, never reordered.
    - **temporal** (``TEMPORAL_ISSUES``): a naive timestamp, a venue clock
      further ahead of ours than ``max_clock_skew_ms`` or further behind than
      ``max_exchange_lag_ms``, a funding interval no venue runs, or a next
      settlement that is already past or further away than one interval.

    Every decision, accepted or not, is folded into ``fingerprint``.
    """

    def __init__(
        self,
        request: DatasetRequest,
        *,
        gap_ms: int,
        funding_gap_ms: int | None = None,
        temporal: TemporalLimits | None = None,
    ) -> None:
        self._request = request
        self._gap = timedelta(milliseconds=gap_ms)
        self._funding_gap = timedelta(milliseconds=funding_gap_ms or gap_ms)
        self._temporal = temporal or TemporalLimits()
        self._state: dict[tuple[MarketRef, EventKind], _StreamState] = {}
        self._initialized: set[tuple[MarketRef, EventKind]] = set()
        self._last_order: tuple[datetime, int, int] | None = None
        self.issues = DatasetIssues()
        self.fingerprint = hashlib.sha256()
        self._digest_request()
        self.events_accepted = 0
        self.events_rejected = 0
        self.initialization_events = 0

    # --- specs and initialization ----------------------------------------

    def _digest_request(self) -> None:
        refs = sorted(
            (
                ref.venue,
                ref.symbol,
                ref.market_type.value,
            )
            for ref in self._request.refs
        )
        parts: list[object] = [
            "request",
            self._request.start.isoformat(),
            self._request.end.isoformat(),
            len(refs),
        ]
        for ref in refs:
            parts.extend(ref)
        self.fingerprint.update(("\x1f".join(str(part) for part in parts) + "\x1e").encode())

    def record_capture_gaps(self, gaps: Sequence[str]) -> None:
        """A window the recorder lost is missing data, however quiet it looks."""
        for gap in gaps:
            self.issues.add("capture_gap", gap)
            self.fingerprint.update(f"capture_gap\x1f{gap}\x1e".encode())

    def record_specs(
        self, specs: Mapping[MarketRef, MarketSpec], versions: Mapping[MarketRef, str]
    ) -> None:
        digest_specs(self.fingerprint, specs, versions)

    def initialize(self, candidates: Sequence[ReplayEvent]) -> list[ReplayEvent]:
        """Pick, per market and kind, the newest valid observation before the start.

        ``candidates`` are newest first per stream. The chosen observation
        becomes the stream's history - a later in-window copy of the same
        book is a duplicate, and a gap is measured from it - but it is not an
        in-window event and is counted separately.
        """
        chosen: list[ReplayEvent] = []
        for event in candidates:
            stream = (event.ref, event.kind)
            if stream in self._initialized:
                continue
            if event.available_at >= self._request.start:
                self._reject(event, "outside_range", "initialization row is not before the start")
                continue
            problem = self._temporal_problem(event)
            if problem is not None:
                self._reject(event, *problem)
                continue
            self._initialized.add(stream)
            chosen.append(event)
        chosen.sort(key=lambda event: event.order_key)
        for event in chosen:
            self._remember(event)
            event.digest_into(self.fingerprint, role="init")
            self.initialization_events += 1
        return chosen

    # --- the stream -------------------------------------------------------

    def corrupt(self, kind: str, detail: str) -> None:
        self.issues.add("corrupt", f"{kind}: {detail}")
        self.events_rejected += 1
        self.fingerprint.update(f"rejected\x1fcorrupt\x1f{kind}\x1f{detail}\x1e".encode())

    def accept(self, event: ReplayEvent) -> bool:
        key = event.order_key
        if self._last_order is not None and key < self._last_order:
            return self._reject(event, "out_of_order", "")
        if not (self._request.start <= event.available_at < self._request.end):
            return self._reject(event, "outside_range", "")
        problem = self._temporal_problem(event)
        if problem is not None:
            return self._reject(event, *problem)
        stream = (event.ref, event.kind)
        state = self._state.get(stream)
        label = f"{event.ref}:{event.kind.name}"
        limit = self._funding_gap if event.kind is EventKind.FUNDING else self._gap
        if state is None:
            self._gap_between(label, self._request.start, event.available_at, limit, leading=True)
        else:
            if self._is_duplicate(state, event):
                return self._reject(event, "duplicate", "")
            if self._is_regression(state, event):
                return self._reject(
                    event, "regression", f"sequence {event.sequence} after {state.last_sequence}"
                )
            self._gap_between(label, state.last_at, event.available_at, limit)
        self._remember(event)
        self._last_order = key
        event.digest_into(self.fingerprint)
        self.events_accepted += 1
        return True

    def finish(self, refs: Sequence[MarketRef]) -> None:
        """Record trailing gaps, and markets whose streams never appeared."""
        kinds_seen: dict[MarketRef, set[EventKind]] = {}
        for (ref, kind), state in self._state.items():
            kinds_seen.setdefault(ref, set()).add(kind)
            limit = self._funding_gap if kind is EventKind.FUNDING else self._gap
            self._gap_between(
                f"{ref}:{kind.name}", state.last_at, self._request.end, limit, trailing=True
            )
        for ref in refs:
            seen = kinds_seen.get(ref, set())
            for kind in (EventKind.QUOTE, EventKind.BOOK):
                if kind not in seen:
                    self.issues.add("missing_stream", f"{ref}:{kind.name}")
            if ref.market_type.value != "SPOT" and EventKind.FUNDING not in seen:
                self.issues.add("missing_stream", f"{ref}:FUNDING")

    # --- rules ------------------------------------------------------------

    def _remember(self, event: ReplayEvent) -> None:
        funding_time = (
            event.payload.next_funding_time if isinstance(event.payload, FundingInfo) else None
        )
        self._state[(event.ref, event.kind)] = _StreamState(
            last_at=event.available_at,
            last_sequence=event.sequence,
            last_funding_time=funding_time,
        )

    def _reject(self, event: ReplayEvent, kind: str, detail: str) -> bool:
        where = f"{event.ref}:{event.kind.name} at {event.available_at.isoformat()}"
        self.issues.add(kind, f"{where} {detail}".rstrip())
        self.events_rejected += 1
        event.digest_into(self.fingerprint, role=f"rejected:{kind}:{detail}")
        return False

    def _temporal_problem(self, event: ReplayEvent) -> tuple[str, str] | None:
        limits = self._temporal
        payload = event.payload
        stamps: list[datetime | None] = [event.available_at, payload.local_timestamp]
        if isinstance(payload, Quote | OrderBook):
            stamps.append(payload.exchange_timestamp)
        if isinstance(payload, FundingInfo):
            stamps.append(payload.next_funding_time)
        if any(stamp is not None and stamp.utcoffset() is None for stamp in stamps):
            return "naive_timestamp", "a timestamp without a timezone cannot be ordered"
        if isinstance(payload, Quote | OrderBook) and payload.exchange_timestamp is not None:
            lag_ms = (payload.local_timestamp - payload.exchange_timestamp) / timedelta(
                milliseconds=1
            )
            if lag_ms < -limits.max_clock_skew_ms:
                return (
                    "future_exchange_timestamp",
                    f"venue clock {-lag_ms:.0f} ms ahead of receipt",
                )
            if lag_ms > limits.max_exchange_lag_ms:
                return "implausible_exchange_lag", f"venue clock {lag_ms:.0f} ms behind receipt"
        if isinstance(payload, FundingInfo):
            hours = payload.funding_interval_hours
            if hours is not None and hours not in VALID_FUNDING_INTERVALS:
                return "invalid_funding_interval", f"{hours} h is not a venue funding interval"
            tolerance = timedelta(milliseconds=limits.funding_schedule_tolerance_ms)
            until = payload.next_funding_time - payload.local_timestamp
            horizon = timedelta(hours=hours if hours is not None else 24)
            if until < -tolerance or until > horizon + tolerance:
                return (
                    "invalid_funding_schedule",
                    f"next settlement {payload.next_funding_time.isoformat()} is "
                    f"{until.total_seconds():.0f} s from the observation",
                )
        return None

    def _is_duplicate(self, state: _StreamState, event: ReplayEvent) -> bool:
        if event.sequence is not None and state.last_sequence is not None:
            return event.sequence == state.last_sequence
        return event.available_at == state.last_at

    def _is_regression(self, state: _StreamState, event: ReplayEvent) -> bool:
        if (
            event.sequence is not None
            and state.last_sequence is not None
            and event.sequence < state.last_sequence
        ):
            return True
        payload = event.payload
        return (
            isinstance(payload, FundingInfo)
            and state.last_funding_time is not None
            and payload.next_funding_time < state.last_funding_time
        )

    def _gap_between(
        self,
        label: str,
        earlier: datetime,
        later: datetime,
        limit: timedelta,
        *,
        leading: bool = False,
        trailing: bool = False,
    ) -> None:
        span = later - earlier
        if span <= limit:
            return
        milliseconds = int(span.total_seconds() * 1000)
        where = "leading " if leading else "trailing " if trailing else ""
        self.issues.add(
            "gap", f"{label} {where}gap of {milliseconds} ms from {earlier.isoformat()}"
        )
        self.issues.longest_gap_ms[label] = max(
            self.issues.longest_gap_ms.get(label, 0), milliseconds
        )
