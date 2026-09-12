"""The risk engine (Phase 9): the gate every signal crosses before execution.

```
Signal -> RiskEngine.evaluate -> APPROVED -> bounded execution dispatcher
                               -> REJECTED -> durable risk event, no order
                               -> PAUSED   -> durable state, no order
```

See ``docs/risk-management.md`` for what is and is not enforced, and
``trading_bot.risk.engine.RiskEngine`` for the implementation.
"""

from __future__ import annotations

from trading_bot.risk.engine import RiskEngine
from trading_bot.risk.kill_switch import KillSwitchState
from trading_bot.risk.models import (
    NullPnlSource,
    PnlSource,
    PostTradeFinding,
    RiskEventDraft,
    RiskVerdict,
)
from trading_bot.risk.store import RiskEventStore

__all__ = [
    "KillSwitchState",
    "NullPnlSource",
    "PnlSource",
    "PostTradeFinding",
    "RiskEngine",
    "RiskEventDraft",
    "RiskEventStore",
    "RiskVerdict",
]
