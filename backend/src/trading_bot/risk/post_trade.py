"""What an execution attempt actually did, versus what was approved.

Split out of ``risk.engine`` because it is a genuinely separate question:
the pre-trade gate reasons about a signal before anything happens, this
module reasons about fills, slippage and timing after something did. Pure
functions over ``ExecutionAttempt`` and ``RiskConfig`` - no store, no kill
switch - so ``RiskEngine.evaluate_post_trade`` stays the only place that
turns a finding into a durable row or a halt.

**What pauses trading, and what only gets recorded.**

| Finding | Response |
| --- | --- |
| Naked exposure (one leg filled, the other did not) | pause (``pause_on_unhedged``) |
| Realised slippage past ``max_slippage_bps`` | pause (``pause_on_abnormal_execution``) |
| Execution latency past ``max_latency_ms`` | pause (``pause_on_abnormal_execution``) |
| Adapter failure or timeout | pause (``pause_on_abnormal_execution``) |
| Cross-leg fill skew past its bound | **recorded only** |

Skew is deliberately the exception. The other four either leave exposure
nobody chose or prove execution is not behaving the way the approval assumed,
and both are cases the architecture's "fail safe" principle exists for. Skew
describes *how simultaneously* two legs filled, and its exposure consequence
is already covered: skew that actually broke the hedge shows up as naked
exposure and pauses under that rule, while skew that did not is a latency
observation worth measuring and not worth halting a strategy over. Phase 8
measured skew on every attempt; halting on it would stop trading for a
condition that had no effect on the book.

Realised slippage is aggregated the same way the pre-trade estimate is - the
sum of *adverse* per-leg slippage, so a leg that filled better than expected
cannot net off one that filled worse.
"""

from __future__ import annotations

from dataclasses import asdict
from decimal import Decimal
from typing import Any

from trading_bot.core.config import RiskConfig
from trading_bot.db.models.enums import OrderStatus, RiskEventType
from trading_bot.execution.account import AccountRejection
from trading_bot.execution.coordinator import ExecutionAttempt
from trading_bot.execution.models import RejectionCode
from trading_bot.risk.models import PausePolicy, PostTradeFinding

#: Outcomes that mean the adapter itself did not work, as opposed to the
#: venue refusing an order for a reason the simulator can explain.
_ADAPTER_FAILURES = frozenset({RejectionCode.ADAPTER_ERROR, RejectionCode.TIMEOUT})


def _decimal(value: int | float | None) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def realised_slippage_bps(attempt: ExecutionAttempt) -> Decimal:
    """Adverse slippage summed across the legs that actually filled."""
    total = Decimal(0)
    for outcome in attempt.legs:
        slippage = outcome.result.slippage_bps
        if slippage is not None and slippage > 0:
            total += slippage
    return total


def findings_for(attempt: ExecutionAttempt, config: RiskConfig) -> list[PostTradeFinding]:
    """Everything about this attempt that is worth a human's attention."""
    findings: list[PostTradeFinding] = []
    if attempt.unhedged_quantity > 0:
        findings.append(
            PostTradeFinding(
                event_type=RiskEventType.ABNORMAL_EXECUTION,
                reason=f"naked exposure of {attempt.unhedged_quantity}: {attempt.describe()}",
                limit_name="unhedged_quantity",
                limit_value=Decimal(0),
                observed_value=attempt.unhedged_quantity,
                pause_policy=PausePolicy.UNHEDGED,
            )
        )

    for outcome in attempt.legs:
        result = outcome.result
        if result.notional > Decimal(str(config.max_order_notional_usd)):
            findings.append(
                PostTradeFinding(
                    event_type=RiskEventType.ORDER_SIZE_EXCEEDED,
                    reason=(
                        f"{outcome.leg.ref.symbol} settled order notional {result.notional} "
                        f"exceeded {config.max_order_notional_usd}"
                    ),
                    limit_name="max_order_notional_usd",
                    limit_value=Decimal(str(config.max_order_notional_usd)),
                    observed_value=result.notional,
                    pause_policy=PausePolicy.ABNORMAL,
                )
            )
        if result.status is OrderStatus.FAILED or result.rejection in _ADAPTER_FAILURES:
            findings.append(
                PostTradeFinding(
                    event_type=RiskEventType.ABNORMAL_EXECUTION,
                    reason=(
                        f"{outcome.leg.ref.symbol} execution did not complete: "
                        f"{result.status.value}"
                        f"{'/' + result.rejection.value if result.rejection else ''}"
                    ),
                    limit_name="adapter_outcome",
                    pause_policy=PausePolicy.ABNORMAL,
                )
            )

    realised = realised_slippage_bps(attempt)
    max_slippage_bps = Decimal(str(config.max_slippage_bps))
    if realised > max_slippage_bps:
        findings.append(
            PostTradeFinding(
                event_type=RiskEventType.SLIPPAGE_EXCEEDED,
                reason=(
                    f"realised slippage across both legs is {realised} bps, "
                    f"over the {max_slippage_bps} bps limit"
                ),
                limit_name="max_slippage_bps",
                limit_value=max_slippage_bps,
                observed_value=realised,
                pause_policy=PausePolicy.ABNORMAL,
            )
        )

    for outcome in attempt.legs:
        latency = outcome.result.terminal_latency_ms or outcome.result.latency_ms
        if latency is not None and latency > config.max_latency_ms:
            findings.append(
                PostTradeFinding(
                    event_type=RiskEventType.LATENCY_EXCEEDED,
                    reason=(
                        f"{outcome.leg.ref.symbol} execution latency {latency}ms "
                        f"exceeded {config.max_latency_ms}ms"
                    ),
                    limit_name="max_latency_ms",
                    limit_value=Decimal(config.max_latency_ms),
                    observed_value=Decimal(latency),
                    pause_policy=PausePolicy.ABNORMAL,
                )
            )

    if attempt.timing_violation:
        findings.append(
            PostTradeFinding(
                event_type=RiskEventType.ABNORMAL_EXECUTION,
                reason=f"cross-leg fill skew {attempt.timing_skew_ms}ms exceeded the bound",
                limit_name="max_leg_skew_ms",
                observed_value=_decimal(attempt.timing_skew_ms),
                # Recorded, never halting on its own - see the module docstring.
                pause_policy=PausePolicy.NONE,
            )
        )
    return findings


def account_limit_finding(breach: AccountRejection) -> PostTradeFinding:
    """A settled account limit crossed because the realised price moved."""
    event_type = (
        RiskEventType.POSITION_LIMIT_EXCEEDED
        if breach.limit_name == "max_position_notional_usd"
        else RiskEventType.EXPOSURE_LIMIT_EXCEEDED
    )
    return PostTradeFinding(
        event_type=event_type,
        reason=breach.detail,
        limit_name=breach.limit_name,
        limit_value=breach.limit_value,
        observed_value=breach.observed_value,
        pause_policy=PausePolicy.ABNORMAL,
    )


def should_pause(findings: list[PostTradeFinding], config: RiskConfig) -> bool:
    """Whether any finding calls for a halt under the configured policies."""
    return any(
        (finding.pause_policy is PausePolicy.UNHEDGED and config.pause_on_unhedged)
        or (finding.pause_policy is PausePolicy.ABNORMAL and config.pause_on_abnormal_execution)
        for finding in findings
    )


def primary(findings: list[PostTradeFinding]) -> PostTradeFinding:
    """The finding that names the row: whichever one can halt trading, first.

    Naked exposure outranks the rest because it is the one that leaves a
    position nobody chose to hold.
    """
    ranked = sorted(
        findings,
        key=lambda finding: (
            0 if finding.pause_policy is PausePolicy.UNHEDGED else 1,
            0 if finding.pause_policy is PausePolicy.ABNORMAL else 1,
        ),
    )
    return ranked[0]


def serialise(finding: PostTradeFinding) -> dict[str, Any]:
    row = asdict(finding)
    row["event_type"] = finding.event_type.value
    row["pause_policy"] = finding.pause_policy.value
    for key in ("limit_value", "observed_value"):
        if row[key] is not None:
            row[key] = str(row[key])
    return row
