"""Writing opportunities and signals to the research record.

Every episode is stored, profitable or not: a dataset containing only the
trades we would have taken cannot say how many chances existed, how many
survived fees, or what killed the rest. Rows here are never purged - unlike
raw market data, they are the point of the exercise.

A row captures the episode at its **best** moment. ``detected_at`` is when the
episode opened, ``best_observed_at`` is when the moment the economics describe
actually happened, and ``last_seen_at`` closes the run - three different facts
that one column cannot hold.

Prices, quantity and the itemised costs are on the row, and ``evidence`` holds
the quotes, books, fills, venue filters, fee rates, funding observation and
cost-model assumptions behind them. That is what makes a row re-derivable
after retention empties ``market_data`` and ``order_books``; a foreign key
into those tables would point at a sampled quote the decision never saw, and
then at nothing.

An episode nobody could price is stored too, with NULL costs, a NULL net edge
and status ``UNPRICEABLE``. It used to be dropped, which silently removed
observations from the count of how many opportunities there were.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import Callable, Sequence
from contextlib import AbstractAsyncContextManager
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from trading_bot.core.logging import get_logger
from trading_bot.db.models import Opportunity as OpportunityRow
from trading_bot.db.models import Order as OrderRow
from trading_bot.db.models import Position as PositionRow
from trading_bot.db.models import RiskEvent as RiskEventRow
from trading_bot.db.models import Signal as SignalRow
from trading_bot.db.models.enums import ExecutionMode, OpportunityStatus, SignalStatus
from trading_bot.db.scope import RunScope
from trading_bot.exchange.models import MarketRef
from trading_bot.opportunities.episodes import OpportunityEpisode
from trading_bot.strategy.models import RejectionReason

logger = get_logger(__name__)

SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]

# Episodes held while the database is unreachable. Beyond this the oldest are
# dropped, with a warning, rather than growing without bound.
MAX_PENDING_EPISODES = 5000

# Stamped on every evidence document. A reader that does not recognise the
# version knows to stop rather than to misread it; a row with no evidence at
# all predates provenance and is not reproducible.
EVIDENCE_SCHEMA = 1


def _status(episode: OpportunityEpisode) -> OpportunityStatus:
    """What became of this opportunity, in the schema's vocabulary."""
    if episode.ever_actionable:
        # It passed every gate the strategy applies. Nothing executed it -
        # that is Phase 8 - so VALIDATED, never EXECUTED.
        return OpportunityStatus.VALIDATED
    if episode.best.edge is None:
        # Detected, real, and never priceable. Not REJECTED: it never reached
        # a cost to be rejected by, and filing it with the priced rejections
        # would answer "how many survived costs?" with a population that was
        # never costed.
        return OpportunityStatus.UNPRICEABLE
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


def _evidence(episode: OpportunityEpisode) -> dict[str, Any] | None:
    """The decision's own inputs and assumptions, as one JSON document.

    Not a foreign key into ``market_data``: those rows are sampled every 5 s
    rather than written per evaluation, so none of them is the quote this
    decision used, and retention deletes them after 7 days while the
    opportunity is kept forever. Carrying the evidence keeps the row
    reproducible after the raw feed behind it is gone.
    """
    item = episode.best
    market = item.opportunity.evidence
    pricing = item.edge.pricing if item.edge is not None else None
    if market is None and pricing is None:
        return None
    document: dict[str, Any] = {
        "schema": EVIDENCE_SCHEMA,
        "episode": {
            "opened_at": episode.opened_at.isoformat(),
            "best_observed_at": episode.best_at.isoformat(),
            "last_seen_at": episode.last_seen_at.isoformat(),
            "samples": episode.samples,
            "rejections": sorted({reason.value for reason in episode.rejections}),
        },
    }
    if market is not None:
        document["market"] = market.as_dict()
    if pricing is not None:
        document["pricing"] = pricing.as_dict()
    return document


def opportunity_row(
    episode: OpportunityEpisode,
    market_ids: dict[MarketRef, int],
    scope: RunScope | None = None,
) -> dict[str, Any] | None:
    """The episode as a row, or ``None`` when its legs are not registered.

    An episode whose costs were never estimable is still stored - it is an
    observation, and dropping it made "how many opportunities were there?"
    unanswerable. What is *not* stored is an invented cost: its cost columns
    and its net edge are NULL and its status is ``UNPRICEABLE``, so the three
    populations - unpriceable, priced and rejected, validated - stay apart.
    """
    item = episode.best
    edge = item.edge
    opportunity = item.opportunity
    buy_id = market_ids.get(opportunity.buy.ref)
    sell_id = market_ids.get(opportunity.sell.ref)
    if buy_id is None or sell_id is None:
        return None
    costs = edge.costs if edge is not None else None
    # Detection is independent of how it would be executed, so a live
    # opportunity is THEORETICAL. A replayed one is BACKTEST and belongs to
    # its run: it describes history, never the market as it is now.
    scope = scope or RunScope(ExecutionMode.THEORETICAL)
    return {
        # Fixed when the episode opened, so a retried flush presents the same
        # row rather than a second observation of the same moment.
        "uid": episode.uid,
        "detected_at": episode.opened_at,
        # The economics below describe this instant, not the opening one.
        "best_observed_at": episode.best_at,
        "last_seen_at": episode.last_seen_at,
        "samples": episode.samples,
        "strategy": episode.strategy,
        **scope.values(),
        "market_id": buy_id,
        "secondary_market_id": sell_id,
        "direction": opportunity.direction,
        "entry_price": opportunity.buy.executable_price,
        # exit_price is deliberately not written: it used to hold the sold
        # leg's *entry* price, which is not an exit of anything.
        "sell_entry_price": opportunity.sell.executable_price,
        "buy_unwind_price": opportunity.buy.unwind_price,
        "sell_unwind_price": opportunity.sell.unwind_price,
        "quantity": opportunity.quantity,
        "notional_usd": opportunity.notional_usd,
        "requested_notional_usd": opportunity.requested_notional_usd,
        "gross_edge_bps": opportunity.gross_edge_bps,
        "gross_edge_usd": opportunity.gross_edge_usd,
        "estimated_fees_usd": costs.fees_usd if costs else None,
        "estimated_slippage_usd": costs.slippage_usd if costs else None,
        # Signed: a short perpetual receives funding when the rate is positive.
        "funding_cost_usd": costs.funding_usd if costs else None,
        "borrow_cost_usd": costs.borrow_usd if costs else None,
        "other_costs_usd": costs.other_usd if costs else None,
        "safety_buffer_usd": costs.buffer_usd if costs else None,
        "net_edge_bps": edge.net_edge_bps if edge else None,
        "net_edge_usd": edge.net_edge_usd if edge else None,
        "liquidity_usd": opportunity.liquidity_usd,
        "latency_ms": opportunity.latency_ms,
        "duration_ms": episode.duration_ms,
        "status": _status(episode),
        "rejection_reason": _rejection_reason(episode),
        "evidence": _evidence(episode),
    }


def signal_rows(
    episode: OpportunityEpisode,
    opportunity_id: int,
    market_ids: dict[MarketRef, int],
    backtest_run_id: int | None = None,
) -> list[dict[str, Any]]:
    """One row per leg - the signals table describes a single market each."""
    signal = episode.executed_signal
    if signal is None and episode.best_actionable is not None:
        signal = episode.best_actionable.signal
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
                "backtest_run_id": backtest_run_id,
                "generated_at": signal.generated_at,
                "strategy": signal.strategy,
                "market_id": market_id,
                "side": leg.side,
                "quantity": leg.quantity,
                "target_entry_price": leg.executable_price,
                # Where THIS leg is closed: its own modelled unwind, against
                # the other side of its own book. The counterpart leg's entry
                # price used to go here, which described no exit at all.
                "target_exit_price": leg.unwind_price,
                "expected_net_edge_bps": signal.expected_net_edge_bps,
                "status": SignalStatus.GENERATED,
                "expires_at": signal.expires_at,
                "reason": None,
            }
        )
    return rows


class OpportunityRecorder:
    """Persists closed episodes, and the signals they produced."""

    def __init__(
        self,
        market_ids: dict[MarketRef, int],
        session_factory: SessionFactory,
        *,
        interval_seconds: float,
        min_duration_ms: int = 0,
        scope: RunScope | None = None,
    ) -> None:
        # Back-links to orders, positions and risk events are filtered by the
        # same run: replayed episode uids repeat across runs by design.
        self._scope = scope or RunScope(ExecutionMode.THEORETICAL)
        self._market_ids = market_ids
        self._session_factory = session_factory
        self._interval = interval_seconds
        self._min_duration_ms = min_duration_ms
        self._pending: list[OpportunityEpisode] = []
        self.opportunities_written = 0
        self.signals_written = 0
        self.unpriced = 0
        self.failures = 0
        # Episodes lost for good (see ``ExecutionRecorder.dropped``).
        self.dropped = 0
        self.unregistered = 0
        self.last_error: str | None = None

    @property
    def pending(self) -> int:
        return len(self._pending)

    def record(self, episodes: Sequence[OpportunityEpisode]) -> None:
        """Queue closed episodes; written with the next flush.

        Queuing the same episode twice - a shutdown closing one already
        recorded, say - must not write it twice, so the queue is keyed by the
        episode's uid. The unique index on that column is the backstop if a
        duplicate ever reaches the database anyway.
        """
        queued = {episode.uid for episode in self._pending}
        for episode in episodes:
            if episode.duration_ms < self._min_duration_ms:
                continue
            if episode.uid in queued:
                continue
            if len(self._pending) >= MAX_PENDING_EPISODES:
                logger.warning("opportunities.episode_dropped", strategy=episode.strategy)
                self._pending.pop(0)
                self.dropped += 1
            self._pending.append(episode)
            queued.add(episode.uid)

    async def flush(self) -> int:
        """Write queued episodes; returns opportunity rows written.

        A database failure keeps the queue and retries next interval: it must
        never stop the strategy that the rest of the platform depends on. The
        retry is safe because each episode's uid was fixed when it opened.
        """
        if not self._pending:
            return 0
        batch, self._pending = self._pending, []
        rows: list[tuple[OpportunityEpisode, dict[str, Any]]] = []
        unregistered = 0
        unpriced = 0
        for episode in batch:
            row = opportunity_row(episode, self._market_ids, self._scope)
            if row is None:
                # Neither leg is in ``markets``; nothing can reference it.
                unregistered += 1
                self.unregistered += 1
                continue
            if row["net_edge_bps"] is None:
                unpriced += 1
            rows.append((episode, row))
        if not rows:
            return 0

        try:
            async with self._session_factory() as session:
                written = await self._write(session, rows)
        except Exception as exc:
            requeued = batch + self._pending
            self.dropped += max(0, len(requeued) - MAX_PENDING_EPISODES)
            self._pending = requeued[-MAX_PENDING_EPISODES:]
            self.failures += 1
            self.last_error = f"{type(exc).__name__}: {exc}"
            logger.warning("opportunities.write_failed", error=str(exc), pending=len(batch))
            return 0

        self.opportunities_written += len(rows)
        self.signals_written += written
        self.unpriced += unpriced
        if unregistered:
            logger.warning("opportunities.market_not_registered", episodes=unregistered)
        _log_unpriced(unpriced, batch)
        return len(rows)

    async def _write(
        self, session: AsyncSession, rows: Sequence[tuple[OpportunityEpisode, dict[str, Any]]]
    ) -> int:
        """Insert the opportunities, then the signals that reference them."""
        # sort_by_parameter_order is not optional here: without it PostgreSQL
        # may return the generated ids in any order, and every signal would be
        # attached to the wrong opportunity - silently, and only under batches.
        values = [row for _, row in rows]
        statement = insert(OpportunityRow).values(values)
        await session.execute(
            statement.on_conflict_do_update(
                constraint="run_opportunity_uid",
                set_={
                    key: getattr(statement.excluded, key)
                    for key in values[0]
                    if key not in {"uid", "backtest_run_id"}
                },
            )
        )
        run_id = self._scope.backtest_run_id
        result = await session.execute(
            select(OpportunityRow.id, OpportunityRow.uid).where(
                self._scope.run_filter(OpportunityRow.backtest_run_id),
                OpportunityRow.uid.in_([episode.uid for episode, _ in rows]),
            )
        )
        ids = {uid: row_id for row_id, uid in result}
        signals: list[dict[str, Any]] = []
        signal_episodes: list[tuple[OpportunityEpisode, dict[str, Any]]] = []
        for episode, _ in rows:
            for signal in signal_rows(episode, ids[episode.uid], self._market_ids, run_id):
                signals.append(signal)
                signal_episodes.append((episode, signal))
        if signals:
            signal_statement = insert(SignalRow).values(signals)
            await session.execute(
                signal_statement.on_conflict_do_update(
                    constraint="opportunity_market_side",
                    set_={
                        key: getattr(signal_statement.excluded, key)
                        for key in signals[0]
                        if key not in {"opportunity_id", "market_id", "side", "backtest_run_id"}
                    },
                )
            )
            stored = await session.execute(
                select(
                    SignalRow.id,
                    SignalRow.opportunity_id,
                    SignalRow.market_id,
                    SignalRow.side,
                ).where(SignalRow.opportunity_id.in_(list(ids.values())))
            )
            signal_ids = {
                (opportunity_id, market_id, side): signal_id
                for signal_id, opportunity_id, market_id, side in stored
            }
            primary_signal: dict[Any, int] = {}
            for episode, signal in signal_episodes:
                signal_id = signal_ids[
                    (signal["opportunity_id"], signal["market_id"], signal["side"])
                ]
                await session.execute(
                    update(OrderRow)
                    .where(
                        OrderRow.opportunity_uid == episode.uid,
                        OrderRow.market_id == signal["market_id"],
                        OrderRow.is_shadow.is_(False),
                        self._scope.run_filter(OrderRow.backtest_run_id),
                    )
                    .values(signal_id=signal_id)
                )
                primary_signal.setdefault(episode.uid, signal_id)
                await session.execute(
                    update(PositionRow)
                    .where(
                        PositionRow.opportunity_uid == episode.uid,
                        PositionRow.market_id == signal["market_id"],
                        self._scope.run_filter(PositionRow.backtest_run_id),
                    )
                    .values(opportunity_id=signal["opportunity_id"])
                )
            await self._link_risk_events(session, primary_signal)
        return len(signals)

    async def _link_risk_events(
        self, session: AsyncSession, primary_signal: dict[Any, int]
    ) -> None:
        """Point each episode's risk decisions at the signal they gated.

        A risk decision covers the whole two-legged intent while a signal row
        describes one leg, so the link names the episode's *first* leg and
        ``opportunity_uid`` stays the complete join. Shadow decisions are left
        unlinked on purpose: a probe's signal is one the strategy declined to
        make, and attaching it to a real signal row would blur exactly the
        line ``is_shadow`` exists to keep.
        """
        for uid, signal_id in primary_signal.items():
            await session.execute(
                update(RiskEventRow)
                .where(
                    RiskEventRow.opportunity_uid == uid,
                    RiskEventRow.is_shadow.is_(False),
                    RiskEventRow.signal_id.is_(None),
                    self._scope.run_filter(RiskEventRow.backtest_run_id),
                )
                .values(signal_id=signal_id)
            )

    async def run(self) -> None:
        while True:
            await asyncio.sleep(self._interval)
            await self.flush()


def _log_unpriced(count: int, batch: Sequence[OpportunityEpisode]) -> None:
    """Unpriceable episodes are stored now, but still worth saying out loud."""
    if not count:
        return
    reasons: Counter[str] = Counter(
        reason.value
        for episode in batch
        if episode.best.edge is None
        for reason in episode.rejections or [RejectionReason.FUNDING_UNKNOWN]
    )
    logger.info("opportunities.unpriceable_stored", episodes=count, reasons=dict(reasons))
