"""Shared enum column types.

These are ``VARCHAR`` columns with a ``CHECK`` constraint, not native
PostgreSQL enum types (``native_enum=False``). The database still rejects an
invalid value, but the status sets can grow as later phases add order states,
risk event types and strategies - adding a value to a native enum requires
``ALTER TYPE``, which cannot run inside a transaction and makes migrations
fragile.

Each type is defined once and reused by every column that needs it, so the
check-constraint definitions stay identical across tables.
"""

from __future__ import annotations

from enum import StrEnum

from sqlalchemy import Enum

from trading_bot.db.models import enums


def _enum(python_enum: type[StrEnum], name: str) -> Enum:
    """Validated string column for ``python_enum``."""
    return Enum(
        python_enum,
        name=name,
        native_enum=False,
        # Reject unknown strings on the Python side too, not just in the DB.
        validate_strings=True,
        # Store the member value ("BUY"), not the member name.
        values_callable=lambda enum_cls: [member.value for member in enum_cls],
    )


MARKET_TYPE = _enum(enums.MarketType, "market_type")
SIDE = _enum(enums.Side, "side")
ORDER_TYPE = _enum(enums.OrderType, "order_type")
TIME_IN_FORCE = _enum(enums.TimeInForce, "time_in_force")
ORDER_STATUS = _enum(enums.OrderStatus, "order_status")
OPPORTUNITY_STATUS = _enum(enums.OpportunityStatus, "opportunity_status")
SIGNAL_STATUS = _enum(enums.SignalStatus, "signal_status")
RISK_DECISION = _enum(enums.RiskDecision, "risk_decision")
RISK_EVENT_TYPE = _enum(enums.RiskEventType, "risk_event_type")
POSITION_STATUS = _enum(enums.PositionStatus, "position_status")
EXECUTION_MODE = _enum(enums.ExecutionMode, "execution_mode")
SYSTEM_EVENT_TYPE = _enum(enums.SystemEventType, "system_event_type")
SEVERITY = _enum(enums.Severity, "severity")
