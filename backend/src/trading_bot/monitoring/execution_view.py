"""Terminal panel for what execution actually did.

Ranked by how recent an attempt is, not by how well it went. The rows worth
reading are the disappointing ones - a leg that missed, depth that ran out, a
resting order that expired - and sorting by success would push exactly those
off the bottom of the screen.

Every row states the realised slippage against what the strategy expected.
That difference is the first direct measurement of whether the cost model has
been telling the truth, which no phase before this one could produce.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

from trading_bot.db.models.enums import OrderStatus
from trading_bot.execution.coordinator import ExecutionAttempt, LegOutcome
from trading_bot.monitoring.display import display_width

_MISSING = "-"
_COLUMNS: tuple[tuple[str, int, str], ...] = (
    ("PAIR", 14, "<"),
    ("KIND", 7, "<"),
    ("LEG", 11, "<"),
    ("SIDE", 5, "<"),
    ("WANTED", 12, ">"),
    ("FILLED", 12, ">"),
    ("PRICE", 13, ">"),
    ("SLIP bps", 9, ">"),
    ("FEE USD", 9, ">"),
    ("STATUS", 18, "<"),
    ("WHY", 26, "<"),
)


def _pad(text: str, width: int, align: str) -> str:
    gap = " " * max(0, width - display_width(text))
    return text + gap if align == "<" else gap + text


def _row(cells: Sequence[str]) -> str:
    return "  ".join(
        _pad(cell, width, align) for cell, (_, width, align) in zip(cells, _COLUMNS, strict=True)
    )


def _plain(value: Decimal | None) -> str:
    return _MISSING if value is None else f"{value.normalize():f}"


def _fixed(value: Decimal | None, places: str = "0.01") -> str:
    return _MISSING if value is None else f"{value.quantize(Decimal(places))}"


def _cells(attempt: ExecutionAttempt, outcome: LegOutcome) -> list[str]:
    result = outcome.result
    return [
        outcome.leg.ref.symbol,
        "shadow" if attempt.is_shadow else "signal",
        outcome.leg.ref.market_type.value.lower(),
        outcome.leg.side.value.lower(),
        _plain(result.request.quantity),
        _plain(result.filled_quantity),
        _plain(result.average_price),
        _fixed(result.slippage_bps),
        _fixed(result.fees_usd, "0.0001"),
        result.status.value.replace("_", " ").lower(),
        _why(outcome),
    ]


def _why(outcome: LegOutcome) -> str:
    """Never leave a non-fill unexplained, even on screen."""
    result = outcome.result
    if result.rejection is None:
        return "" if result.status is OrderStatus.FILLED else _MISSING
    return result.rejection.value.replace("_", " ").lower()


def summary_line(attempts: Sequence[ExecutionAttempt]) -> str:
    """Counts first, because the totals are the finding, not any one row."""
    hedged = sum(a.is_hedged for a in attempts)
    empty = sum(a.is_empty for a in attempts)
    unhedged = len(attempts) - hedged - empty
    shadow = sum(a.is_shadow for a in attempts)
    parts = [f"{len(attempts)} attempts", f"{hedged} hedged"]
    if unhedged:
        # Loud on purpose: naked exposure is the failure this phase exists to
        # measure, and Phase 9 is what will be allowed to do anything about it.
        parts.append(f"{unhedged} UNHEDGED")
    if empty:
        parts.append(f"{empty} filled nothing")
    if shadow:
        parts.append(f"{shadow} shadow probes (not strategy trades)")
    return "   ".join(parts)


def render_execution(attempts: Sequence[ExecutionAttempt], *, max_rows: int | None = None) -> str:
    """The execution panel: newest attempts, both legs of each."""
    header = _row([name for name, _, _ in _COLUMNS])
    lines = [
        f"EXECUTION  paper   {summary_line(attempts)}",
        header,
        "-" * display_width(header),
    ]
    rows: list[str] = []
    for attempt in reversed(attempts):
        rows.extend(_row(_cells(attempt, outcome)) for outcome in attempt.legs)
    shown = rows if max_rows is None else rows[: max(2, max_rows)]
    lines.extend(shown)
    if len(shown) < len(rows):
        lines.append(f"... {len(rows) - len(shown)} more legs")
    return "\n".join(lines)
