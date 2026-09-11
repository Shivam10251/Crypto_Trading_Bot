"""Terminal panel for what the strategies see.

Shows every pair a strategy priced, ranked by the edge that *survives costs* -
not by the widest gross spread, which would put the most spread-crossed,
least tradeable markets at the top. Every cost that ate the edge is a column,
so a row explains its own verdict.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

from trading_bot.monitoring.display import compact, display_width
from trading_bot.strategy.models import DetectionStats, RejectionReason
from trading_bot.strategy.runner import EvaluatedOpportunity, StrategyEvaluation

_MISSING = "-"
_COLUMNS: tuple[tuple[str, int, str], ...] = (
    ("PAIR", 14, "<"),
    ("DIR", 10, "<"),
    ("SPOT MID", 13, ">"),
    ("PERP MID", 13, ">"),
    ("BASIS bps", 10, ">"),
    ("SIZE USD", 9, ">"),
    ("FEES", 7, ">"),
    ("SLIP", 7, ">"),
    ("FUND", 7, ">"),
    ("BUF", 6, ">"),
    ("NET bps", 9, ">"),
    ("HELD s", 7, ">"),
    ("LAT ms", 7, ">"),
    ("VERDICT", 18, "<"),
)


def _pad(text: str, width: int, align: str) -> str:
    gap = " " * max(0, width - display_width(text))
    return text + gap if align == "<" else gap + text


def _row(cells: Sequence[str]) -> str:
    return "  ".join(
        _pad(cell, width, align) for cell, (_, width, align) in zip(cells, _COLUMNS, strict=True)
    )


def _plain(value: Decimal) -> str:
    return f"{value.normalize():f}"


def _bps(value: Decimal | None) -> str:
    return _MISSING if value is None else f"{value:.2f}"


def _cost_bps(cost_usd: Decimal, notional: Decimal) -> str:
    """Costs are shown in bps of notional so they compare with the basis."""
    if notional <= 0:
        return _MISSING
    return f"{cost_usd / notional * Decimal(10_000):.2f}"


def _verdict(item: EvaluatedOpportunity) -> str:
    if item.is_actionable:
        return "TRADEABLE"
    if item.rejection is RejectionReason.BELOW_MIN_EDGE:
        return "below min edge"
    if item.rejection is None:  # pragma: no cover - defensive
        return _MISSING
    return item.rejection.value.replace("_", " ").lower()


def _legs(item: EvaluatedOpportunity) -> tuple[Decimal, Decimal, str]:
    """(spot mid, perp mid, direction) for a two-legged spot/perp opportunity."""
    opportunity = item.opportunity
    spot = next(
        (leg for leg in opportunity.legs if leg.ref.market_type.value == "SPOT"), opportunity.buy
    )
    perp = next(
        (leg for leg in opportunity.legs if leg.ref.market_type.value != "SPOT"), opportunity.sell
    )
    direction = "buy spot" if spot.side.value == "BUY" else "sell spot"
    return spot.reference_price, perp.reference_price, direction


def _cells(item: EvaluatedOpportunity) -> list[str]:
    opportunity = item.opportunity
    spot_mid, perp_mid, direction = _legs(item)
    notional = opportunity.notional_usd
    edge = item.edge
    costs = edge.costs if edge else None
    held = _MISSING if opportunity.duration_ms is None else f"{opportunity.duration_ms / 1000:.1f}"
    return [
        opportunity.buy.ref.symbol,
        direction,
        _plain(spot_mid),
        _plain(perp_mid),
        _bps(opportunity.gross_edge_bps),
        compact(notional),
        _MISSING if costs is None else _cost_bps(costs.fees_usd, notional),
        _MISSING if costs is None else _cost_bps(costs.slippage_usd, notional),
        _MISSING if costs is None else _cost_bps(costs.funding_usd, notional),
        _MISSING if costs is None else _cost_bps(costs.buffer_usd, notional),
        _bps(edge.net_edge_bps if edge else None),
        held,
        _MISSING if opportunity.latency_ms is None else str(opportunity.latency_ms),
        _verdict(item),
    ]


def _stats_line(stats: DetectionStats) -> str:
    parts = [f"{stats.pairs_usable}/{stats.pairs_seen} pairs usable"]
    if stats.no_basis:
        parts.append(f"{stats.no_basis} with no basis")
    for reason, count in sorted(stats.unusable.items(), key=lambda item: -item[1]):
        parts.append(f"{count} {reason.value.replace('_', ' ').lower()}")
    return "   ".join(parts)


def render_evaluation(
    evaluation: StrategyEvaluation,
    *,
    cost_summary: str,
    max_rows: int | None = None,
    funding_unknown: Sequence[str] = (),
) -> str:
    """One strategy's view, ranked by net edge - what survives, not what glitters."""
    ranked = sorted(
        evaluation.opportunities,
        key=lambda item: item.edge.net_edge_bps if item.edge else Decimal("-9" * 9),
        reverse=True,
    )
    actionable = len(evaluation.actionable)
    best = evaluation.best()
    best_text = (
        "none priced"
        if best is None or best.edge is None
        else f"{best.edge.net_edge_bps:+.2f} bps ({best.opportunity.buy.ref.symbol})"
    )
    lines = [
        f"STRATEGY {evaluation.strategy}   "
        f"{len(evaluation.opportunities)} priced   "
        f"tradeable {actionable}   best net {best_text}",
        f"costs: {cost_summary}",
    ]
    if evaluation.stats is not None:
        lines.append(_stats_line(evaluation.stats))
    if funding_unknown:
        shown = ", ".join(sorted(funding_unknown)[:6])
        lines.append(
            f"funding interval not published for {len(funding_unknown)} market(s) - "
            f"not priced rather than assumed: {shown}"
        )
    lines.append("")
    header = _row([title for title, _, _ in _COLUMNS])
    lines += [header, "-" * display_width(header)]
    shown_rows = ranked if max_rows is None else ranked[: max(0, max_rows)]
    lines += [_row(_cells(item)) for item in shown_rows]
    if len(shown_rows) < len(ranked):
        lines.append(f"... {len(ranked) - len(shown_rows)} more pairs")
    if not ranked:
        lines.append("  no pair had both legs live with a synchronised book and a non-zero basis")
    return "\n".join(lines)
