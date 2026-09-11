"""Writing opportunities and signals to the research record.

Every episode is stored, profitable or not: a dataset containing only the
trades we would have taken cannot say how many chances existed, how many
survived fees, or what killed the rest. Rows here are never purged - unlike
raw market data, they are the point of the exercise.

A row captures the episode at its **best** moment, with ``detected_at`` and
``duration_ms`` bounding when that was. Prices, quantity and the itemised costs
are all on the row, so a better fee model in a later phase can re-derive net
edge from what is stored. Slippage cannot be re-derived that way - it came from
a book that retention will delete - so it is kept as its own column rather than
folded into the price.
"""

from __future__ import annotations

import asyncio
import uuid
from collections import Counter
from collections.abc import Callable, Sequence
from contextlib import AbstractAsyncContextManager
from decimal import Decimal
from typing import Any

from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncSession

from trading_bot.core.logging import get_logger
from trading_bot.db.models import Opportunity as OpportunityRow
from trading_bot.db.models import Signal as SignalRow
from trading_bot.db.models.enums import ExecutionMode, OpportunityStatus, SignalStatus
from trading_bot.exchange.models import MarketRef
from trading_bot.opportunities.episodes import OpportunityEpisode
from trading_bot.strategy.models import Leg, RejectionReason

logger = get_logger(__name__)

SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]

# Episodes held while the database is unreachable. Beyond this the oldest are
# dropped, with a warning, rather than growing without bound.
MAX_PENDING_EPISODES = 5000


def _status(episode: OpportunityEpisode) -> OpportunityStatus:
    """What became of this opportunity, in the schema's vocabulary."""
    if episode.ever_actionable:
        # It passed every gate the strategy applies. Nothing executed it -
        # that is Phase 8 - so VALIDATED, never EXECUTED.
        return OpportunityStatus.VALIDATED
    if episode.rejections:
        return OpportunityStatus.REJECTED
    return OpportunityStatus.DETECTED


def _rejection_reason(episode: OpportunityEpisode) -> str | None:
    """Never leave a rejection unexplained; list every gate it failed.

    Sorted, not in the order the gates happened to fail: research groups by
    this column, and "A, B" and "B, A" landing in separate buckets would split
    the same population in two for no reason anyone cares about.
    """
    if episode.ever_actionable or not episode.rejections:
        return None
    return ", ".join(sorted({reason.value for reason in episode.rejections}))


def opportunity_row(
    episode: OpportunityEpisode, market_ids: dict[MarketRef, int]
) -> dict[str, Any] | None:
    """The episode as a row, or ``None`` when it cannot be stored honestly.

    An episode whose costs were never estimable has no net edge, and the column
    is NOT NULL for good reason - a fabricated zero would pollute every query
    that asks what survived costs. Those episodes are counted instead.
    """
    item = episode.best
    edge = item.edge
    if edge is None:
        return None
    opportunity = item.opportunity
    buy_id = market_ids.get(opportunity.buy.ref)
    sell_id = market_ids.get(opportunity.sell.ref)
    if buy_id is None or sell_id is None:
        return None
    costs = edge.costs
    return {
        "uid": uuid.uuid4(),
        "detected_at": episode.opened_at,
        "strategy": episode.strategy,
        # Detection is independent of how it would be executed.
        "mode": ExecutionMode.THEORETICAL,
        "market_id": buy_id,
        "secondary_market_id": sell_id,
        "direction": opportunity.direction,
        "entry_price": opportunity.buy.executable_price,
        "exit_price": opportunity.sell.executable_price,
        "quantity": opportunity.quantity,
        "notional_usd": opportunity.notional_usd,
        "gross_edge_bps": opportunity.gross_edge_bps,
        "gross_edge_usd": opportunity.gross_edge_usd,
        "estimated_fees_usd": costs.fees_usd,
        "estimated_slippage_usd": costs.slippage_usd,
        # Signed: a short perpetual receives funding when the rate is positive.
        "funding_cost_usd": costs.funding_usd,
        "borrow_cost_usd": Decimal(0),
        "other_costs_usd": costs.other_usd,
        "safety_buffer_usd": costs.buffer_usd,
        "net_edge_bps": edge.net_edge_bps,
        "net_edge_usd": edge.net_edge_usd,
        "liquidity_usd": opportunity.liquidity_usd,
        "latency_ms": opportunity.latency_ms,
        "duration_ms": episode.duration_ms,
        "status": _status(episode),
        "rejection_reason": _rejection_reason(episode),
    }


def signal_rows(
    episode: OpportunityEpisode, opportunity_id: int, market_ids: dict[MarketRef, int]
) -> list[dict[str, Any]]:
    """One row per leg - the signals table describes a single market each."""
    signal = episode.best.signal
    if signal is None:
        return []
    rows: list[dict[str, Any]] = []
    for leg in signal.legs:
        market_id = market_ids.get(leg.ref)
        if market_id is None:  # pragma: no cover - the opportunity row needs both
            continue
        rows.append(
            {
                "opportunity_id": opportunity_id,
                "generated_at": signal.generated_at,
                "strategy": signal.strategy,
                "market_id": market_id,
                "side": leg.side,
                "quantity": leg.quantity,
                "target_entry_price": leg.executable_price,
                "target_exit_price": _counterpart(signal.legs, leg),
                "expected_net_edge_bps": signal.expected_net_edge_bps,
                "status": SignalStatus.GENERATED,
                "expires_at": signal.expires_at,
                "reason": None,
            }
        )
    return rows


def _counterpart(legs: Sequence[Leg], leg: Leg) -> Decimal | None:
    """The other leg's price - where this position is closed against."""
    return next((other.executable_price for other in legs if other.ref != leg.ref), None)


class OpportunityRecorder:
    """Persists closed episodes, and the signals they produced."""

    def __init__(
        self,
        market_ids: dict[MarketRef, int],
        session_factory: SessionFactory,
        *,
        interval_seconds: float,
        min_duration_ms: int = 0,
    ) -> None:
        self._market_ids = market_ids
        self._session_factory = session_factory
        self._interval = interval_seconds
        self._min_duration_ms = min_duration_ms
        self._pending: list[OpportunityEpisode] = []
        self.opportunities_written = 0
        self.signals_written = 0
        self.unpriced = 0
        self.failures = 0

    def record(self, episodes: Sequence[OpportunityEpisode]) -> None:
        """Queue closed episodes; written with the next flush."""
        for episode in episodes:
            if episode.duration_ms < self._min_duration_ms:
                continue
            if len(self._pending) >= MAX_PENDING_EPISODES:
                logger.warning("opportunities.episode_dropped", strategy=episode.strategy)
                self._pending.pop(0)
            self._pending.append(episode)

    async def flush(self) -> int:
        """Write queued episodes; returns opportunity rows written.

        A database failure keeps the queue and retries next interval: it must
        never stop the strategy that the rest of the platform depends on.
        """
        if not self._pending:
            return 0
        batch = list(self._pending)
        rows: list[tuple[OpportunityEpisode, dict[str, Any]]] = []
        unpriced = 0
        for episode in batch:
            row = opportunity_row(episode, self._market_ids)
            if row is None:
                unpriced += 1
                continue
            rows.append((episode, row))
        if not rows:
            del self._pending[: len(batch)]
            self.unpriced += unpriced
            _log_unpriced(unpriced, batch)
            return 0

        try:
            async with self._session_factory() as session:
                written = await self._write(session, rows)
        except Exception as exc:
            self.failures += 1
            logger.warning("opportunities.write_failed", error=str(exc), pending=len(batch))
            return 0

        del self._pending[: len(batch)]
        self.opportunities_written += len(rows)
        self.signals_written += written
        self.unpriced += unpriced
        _log_unpriced(unpriced, batch)
        return len(rows)

    async def _write(
        self, session: AsyncSession, rows: Sequence[tuple[OpportunityEpisode, dict[str, Any]]]
    ) -> int:
        """Insert the opportunities, then the signals that reference them."""
        # sort_by_parameter_order is not optional here: without it PostgreSQL
        # may return the generated ids in any order, and every signal would be
        # attached to the wrong opportunity - silently, and only under batches.
        result = await session.execute(
            insert(OpportunityRow).returning(OpportunityRow.id, sort_by_parameter_order=True),
            [row for _, row in rows],
        )
        ids = [row_id for (row_id,) in result]
        signals: list[dict[str, Any]] = []
        for (episode, _), opportunity_id in zip(rows, ids, strict=True):
            signals.extend(signal_rows(episode, opportunity_id, self._market_ids))
        if signals:
            await session.execute(insert(SignalRow), signals)
        return len(signals)

    async def run(self) -> None:
        while True:
            await asyncio.sleep(self._interval)
            await self.flush()


def _log_unpriced(count: int, batch: Sequence[OpportunityEpisode]) -> None:
    """An episode that could never be priced is dropped, so say so out loud."""
    if not count:
        return
    reasons: Counter[str] = Counter(
        reason.value
        for episode in batch
        if episode.best.edge is None
        for reason in episode.rejections or [RejectionReason.FUNDING_UNKNOWN]
    )
    logger.warning("opportunities.unpriced_not_stored", episodes=count, reasons=dict(reasons))
