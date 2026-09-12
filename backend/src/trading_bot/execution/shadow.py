"""Deliberate probes, so the simulator is exercised by a market rather than a mock.

Measured across 447 recorded opportunities, **none** passed validation on this
account: the basis is almost always in the direction that needs spot sold
short, and what is left does not clear costs. Wired the intended way, the
paper engine would therefore place nothing, ever, and its fill, partial-fill
and expiry paths would be exercised only by unit tests against books we wrote
ourselves.

A shadow probe closes that gap: once per interval, the best *reachable*
opportunity is simulated even though the strategy rejected it. That measures
the two things nothing before Phase 8 could - what a round trip actually
costs, and how often the two legs fail to agree.

**A probe is not a trade.** It is flagged on the row (`orders.is_shadow`) and
must be excluded from any question about what the strategy would have earned;
including it would answer "how did the strategy do?" with trades the strategy
explicitly declined to make. It is off by default.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from trading_bot.core.logging import get_logger
from trading_bot.db.models.enums import MarketType
from trading_bot.strategy.models import Signal
from trading_bot.strategy.runner import EvaluatedOpportunity, StrategyEvaluation

logger = get_logger(__name__)


def reachable(item: EvaluatedOpportunity, *, allow_spot_short: bool) -> bool:
    """Whether this direction could be placed at all from a cash account.

    Selling spot needs inventory or a margin borrow. Probing a direction the
    account cannot reach would measure nothing except the refusal we already
    know about.
    """
    if allow_spot_short:
        return True
    return item.opportunity.sell.ref.market_type is not MarketType.SPOT


def choose_probe(
    evaluations: list[StrategyEvaluation], *, allow_spot_short: bool
) -> tuple[StrategyEvaluation, EvaluatedOpportunity] | None:
    """The best priced, reachable opportunity this cycle, or nothing.

    "Best" is by net edge, so the probe measures the case that came closest
    to being tradeable rather than an arbitrary one. An unpriceable
    opportunity is never probed: if the cost model would not price it, there
    is nothing to compare a fill against.
    """
    best: tuple[StrategyEvaluation, EvaluatedOpportunity] | None = None
    for evaluation in evaluations:
        for item in evaluation.opportunities:
            if (
                item.edge is None
                or item.is_actionable
                or item.rejection is None
                or not reachable(item, allow_spot_short=allow_spot_short)
            ):
                continue
            incumbent = best[1].edge if best is not None else None
            if incumbent is None or item.edge.net_edge_bps > incumbent.net_edge_bps:
                best = (evaluation, item)
    return best


def probe_signal(
    evaluation: StrategyEvaluation, item: EvaluatedOpportunity, now: datetime, ttl: timedelta
) -> Signal | None:
    """The rejected opportunity as a signal, purely so it can be simulated.

    Built here rather than by the strategy on purpose: the strategy declined
    this trade, and a strategy that could be talked into signalling something
    it rejected would not be worth testing. What is stored keeps the two
    apart - this one is written with ``is_shadow`` set.
    """
    edge = item.edge
    if edge is None:  # pragma: no cover - choose_probe filters these out
        return None
    return Signal(
        strategy=evaluation.strategy,
        generated_at=now,
        opportunity=item.opportunity,
        edge=edge,
        expected_net_edge_bps=edge.net_edge_bps,
        expires_at=now + ttl,
    )
