"""Enumerations shared by the data model.

Stored as ``VARCHAR`` columns with a database ``CHECK`` constraint listing the
allowed values (see ``enum_types``), not as native PostgreSQL enum types: the
database still rejects an invalid value outright, which matters for columns
like order status where a typo would silently corrupt the audit trail.

Adding a member here is a schema change. The CHECK constraints list the
values explicitly, so a new member needs a migration that recreates every
constraint on a column of that type - ``tests/unit/test_enum_constraints.py``
and the migration parity test fail until it has one.
"""

from __future__ import annotations

from enum import StrEnum


class MarketType(StrEnum):
    """Instrument class. The first strategy trades SPOT against PERPETUAL."""

    SPOT = "SPOT"
    PERPETUAL = "PERPETUAL"
    FUTURE = "FUTURE"


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(StrEnum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"


class TimeInForce(StrEnum):
    GTC = "GTC"
    IOC = "IOC"
    FOK = "FOK"


class OrderStatus(StrEnum):
    """Lifecycle of an order, paper or live."""

    PENDING = "PENDING"
    SUBMITTED = "SUBMITTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    FAILED = "FAILED"


class OpportunityStatus(StrEnum):
    """Every opportunity is stored, including the ones never traded."""

    DETECTED = "DETECTED"
    VALIDATED = "VALIDATED"
    REJECTED = "REJECTED"
    # Detected and real, but a cost could not be estimated - an unpublished
    # funding interval, or no depth to price the unwind against. Kept as its
    # own outcome: counting it as REJECTED would put it in the population of
    # opportunities that were priced and did not survive, which it never was.
    UNPRICEABLE = "UNPRICEABLE"
    PAPER_TRADE = "PAPER_TRADE"
    EXPIRED = "EXPIRED"
    EXECUTED = "EXECUTED"
    FAILED = "FAILED"


class SignalStatus(StrEnum):
    GENERATED = "GENERATED"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXECUTED = "EXECUTED"
    EXPIRED = "EXPIRED"


class RiskDecision(StrEnum):
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    PAUSED = "PAUSED"


class RiskEventType(StrEnum):
    """Why the risk engine acted. Drives the research query "where did we stop?"."""

    PRE_TRADE_CHECK = "PRE_TRADE_CHECK"
    STALE_DATA = "STALE_DATA"
    LATENCY_EXCEEDED = "LATENCY_EXCEEDED"
    SLIPPAGE_EXCEEDED = "SLIPPAGE_EXCEEDED"
    ORDER_SIZE_EXCEEDED = "ORDER_SIZE_EXCEEDED"
    POSITION_LIMIT_EXCEEDED = "POSITION_LIMIT_EXCEEDED"
    EXPOSURE_LIMIT_EXCEEDED = "EXPOSURE_LIMIT_EXCEEDED"
    # Cash, spot inventory, perpetual margin or borrow capacity fell short.
    INSUFFICIENT_RESOURCES = "INSUFFICIENT_RESOURCES"
    # Required evidence (a leg's quote, book or the funding observation) is
    # missing or the book has not synced - distinct from merely being old.
    INCOMPLETE_MARKET_DATA = "INCOMPLETE_MARKET_DATA"
    SIGNAL_EXPIRED = "SIGNAL_EXPIRED"
    QUEUE_OVERLOAD = "QUEUE_OVERLOAD"
    DAILY_LOSS_LIMIT = "DAILY_LOSS_LIMIT"
    CONSECUTIVE_LOSSES = "CONSECUTIVE_LOSSES"
    ABNORMAL_EXECUTION = "ABNORMAL_EXECUTION"
    KILL_SWITCH = "KILL_SWITCH"
    # Phase 10. An exit decision: which condition asked for the close, and
    # what the current books priced it at. Recorded for every close attempt,
    # unlike an entry approval, because a close is the one action the kill
    # switch does not gate and its audit trail is the only record of it.
    POSITION_EXIT = "POSITION_EXIT"
    # A close request that would have increased, reversed or exceeded the
    # exposure it claimed to reduce. Refused before any order exists.
    REDUCE_ONLY_VIOLATION = "REDUCE_ONLY_VIOLATION"
    # Database or risk-state uncertainty: refused rather than guessed.
    FAIL_CLOSED = "FAIL_CLOSED"


class PositionStatus(StrEnum):
    OPEN = "OPEN"
    # An exit has been claimed for this position and may be in flight. The
    # exposure still exists - a CLOSING position is restored into the paper
    # account exactly like an OPEN one - but no second worker may claim it.
    CLOSING = "CLOSING"
    CLOSED = "CLOSED"
    LIQUIDATED = "LIQUIDATED"


#: Exposure that still exists and must be valued, reserved against and closed.
LIVE_POSITION_STATUSES = (PositionStatus.OPEN, PositionStatus.CLOSING)


class ValuationStatus(StrEnum):
    """Whether a portfolio snapshot could value every position it counted.

    A snapshot is never silently computed from a stale or missing mark: it
    either values everything from a synchronised book (``COMPLETE``), says
    which part it could not (``DEGRADED``), or declines to publish a number
    at all (``UNAVAILABLE``, with NULL position value and equity).
    """

    COMPLETE = "COMPLETE"
    DEGRADED = "DEGRADED"
    UNAVAILABLE = "UNAVAILABLE"


class ExecutionMode(StrEnum):
    """Which engine produced a row.

    THEORETICAL (what the strategy thought was available), PAPER (simulated
    execution against the live book), LIVE (real money) and BACKTEST
    (simulated execution against recorded history) must never be aggregated
    together.

    BACKTEST alone is not isolation: two backtests over the same period share
    this value. Every BACKTEST row also carries ``backtest_run_id``, and a
    CHECK constraint ties the two together - see ``db.scope.RunScope``.
    """

    THEORETICAL = "THEORETICAL"
    PAPER = "PAPER"
    LIVE = "LIVE"
    BACKTEST = "BACKTEST"


class BacktestRunStatus(StrEnum):
    """Lifecycle of one backtest run.

    ``INCOMPLETE`` is a finished run whose dataset could not support
    everything asked of it - a selected market with no depth, a perpetual with
    no funding observations, or gaps the replay refused to carry a book
    across. Its numbers exist, and they describe less than was requested.
    """

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    INCOMPLETE = "INCOMPLETE"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


#: A run in one of these states will never change again.
TERMINAL_BACKTEST_STATUSES = (
    BacktestRunStatus.COMPLETED,
    BacktestRunStatus.INCOMPLETE,
    BacktestRunStatus.FAILED,
    BacktestRunStatus.CANCELLED,
)


class SystemEventType(StrEnum):
    STARTUP = "STARTUP"
    SHUTDOWN = "SHUTDOWN"
    WS_CONNECTED = "WS_CONNECTED"
    WS_DISCONNECTED = "WS_DISCONNECTED"
    WS_RECONNECTED = "WS_RECONNECTED"
    STALE_DATA = "STALE_DATA"
    DATA_GAP = "DATA_GAP"
    API_ERROR = "API_ERROR"
    DATABASE_ERROR = "DATABASE_ERROR"
    RECONCILIATION = "RECONCILIATION"


class Severity(StrEnum):
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"
