"""Turning a stream of evaluations into opportunities worth recording.

A strategy re-evaluates every market every second, so the same discrepancy is
seen again and again. Writing one row per evaluation would store 4 million rows
a day (measured: 8,378 rows in a 180 s run across 50 pairs) - almost all of
them restating the previous second.

An opportunity is therefore an **episode**: one contiguous period during which
the same strategy sees the same discrepancy in the same direction. It opens
when the discrepancy appears, absorbs every evaluation while it lasts, and
closes when the direction flips or the pair stops being priceable. The same
run produced 168 episodes - fifty times fewer rows, and a far better answer to
"how many opportunities existed?" than a count of samples would be.

The row keeps the episode's **best** moment, not its first: if the peak never
cleared costs, no moment did, and ``detected_at`` with ``duration_ms`` bound
when it happened. Nothing is filtered - unprofitable episodes are the dataset.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from trading_bot.exchange.models import MarketRef
from trading_bot.strategy.models import RejectionReason
from trading_bot.strategy.runner import EvaluatedOpportunity, StrategyEvaluation

# (strategy, bought market, sold market). A basis that flips sign swaps the two
# refs, which is a different trade and so a different episode.
EpisodeKey = tuple[str, MarketRef, MarketRef]


def episode_key(strategy: str, item: EvaluatedOpportunity) -> EpisodeKey:
    return (strategy, item.opportunity.buy.ref, item.opportunity.sell.ref)


@dataclass
class OpportunityEpisode:
    """One discrepancy, from the moment it appeared until it went away."""

    key: EpisodeKey
    strategy: str
    opened_at: datetime
    last_seen_at: datetime
    # The highest-net-edge evaluation seen, and when it happened.
    best: EvaluatedOpportunity
    best_at: datetime
    samples: int = 1
    # True if any evaluation produced a signal that passed validation.
    ever_actionable: bool = False
    # Every reason this episode was turned down, in the order first seen.
    rejections: list[RejectionReason] = field(default_factory=list)

    @property
    def duration_ms(self) -> int:
        """First observation to last, so a **lower bound** on how long it held.

        The discrepancy existed for some unknown time before we first sampled
        it and after we last did; measuring between observations understates
        that rather than inventing the gap. An episode seen once is 0 ms - we
        saw it at one instant and can say nothing more.
        """
        return int((self.last_seen_at - self.opened_at).total_seconds() * 1000)

    @property
    def best_net_edge_bps(self) -> Decimal | None:
        return self.best.net_edge_bps

    def absorb(self, item: EvaluatedOpportunity, now: datetime) -> None:
        self.samples += 1
        self.last_seen_at = now
        if item.is_actionable:
            self.ever_actionable = True
        if item.rejection is not None and item.rejection not in self.rejections:
            self.rejections.append(item.rejection)
        if _is_better(item, self.best):
            self.best = item
            self.best_at = now


def _is_better(candidate: EvaluatedOpportunity, incumbent: EvaluatedOpportunity) -> bool:
    """A priced evaluation always beats an unpriced one; then net edge decides."""
    if candidate.edge is None:
        return False
    if incumbent.edge is None:
        return True
    return candidate.edge.net_edge_bps > incumbent.edge.net_edge_bps


class EpisodeTracker:
    """Keeps the open episodes and hands back the ones that just ended."""

    def __init__(self) -> None:
        self._open: dict[EpisodeKey, OpportunityEpisode] = {}
        self.opened = 0
        self.closed = 0

    def open_episodes(self) -> list[OpportunityEpisode]:
        return list(self._open.values())

    def update(
        self, evaluations: Sequence[StrategyEvaluation], now: datetime
    ) -> list[OpportunityEpisode]:
        """Absorb one cycle; return the episodes that ended in it."""
        seen: set[EpisodeKey] = set()
        for evaluation in evaluations:
            for item in evaluation.opportunities:
                key = episode_key(evaluation.strategy, item)
                seen.add(key)
                episode = self._open.get(key)
                if episode is None:
                    self._open[key] = _start(key, evaluation.strategy, item, now)
                    self.opened += 1
                else:
                    episode.absorb(item, now)
        # Anything not seen this cycle has ended: the direction flipped, a leg
        # went stale, or the discrepancy simply closed.
        ended = [self._open.pop(key) for key in list(self._open) if key not in seen]
        self.closed += len(ended)
        return ended

    def close_all(self) -> list[OpportunityEpisode]:
        """End every open episode - used on shutdown so nothing is lost."""
        ended = list(self._open.values())
        self._open.clear()
        self.closed += len(ended)
        return ended


def _start(
    key: EpisodeKey, strategy: str, item: EvaluatedOpportunity, now: datetime
) -> OpportunityEpisode:
    return OpportunityEpisode(
        key=key,
        strategy=strategy,
        opened_at=now,
        last_seen_at=now,
        best=item,
        best_at=now,
        ever_actionable=item.is_actionable,
        rejections=[item.rejection] if item.rejection is not None else [],
    )
