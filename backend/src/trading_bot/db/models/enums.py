"""Enumerations shared by the data model.

These are stored as native PostgreSQL enum types: the database rejects an
invalid value outright, which matters for columns like order status where a
typo would silently corrupt the audit trail.
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
    # Database or risk-state uncertainty: refused rather than guessed.
    FAIL_CLOSED = "FAIL_CLOSED"


class PositionStatus(StrEnum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"
    LIQUIDATED = "LIQUIDATED"


class ExecutionMode(StrEnum):
    """Which engine produced a row.

    THEORETICAL (what the strategy thought was available), PAPER (simulated
    execution) and LIVE (real money) must never be aggregated together.
    """

    THEORETICAL = "THEORETICAL"
    PAPER = "PAPER"
    LIVE = "LIVE"


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
