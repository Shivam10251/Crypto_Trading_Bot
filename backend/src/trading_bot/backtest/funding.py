"""Signed funding a replayed perpetual leg actually paid or received.

A USD-M perpetual settles funding at fixed instants: at each one, a position
open at that instant pays ``quantity x mark x rate`` if long and receives it
if short (the reverse when the rate is negative). Attributing it from a
recording therefore needs four facts per settlement, and **every** settlement
a leg crossed must have all four, or the leg's funding is ``None`` -
unmeasured - rather than a partial sum presented as the total:

1. **When it settled.** The schedule comes from the recorded
   ``next_funding_time`` and interval. A leg's settlements are those in
   ``(first entry fill, last exit fill]`` - the same half-open convention the
   cost model charges by. Observations disagreeing about the interval inside
   that window make the schedule ambiguous, and funding unmeasured.
2. **The rate it settled at.** The venue's rate is an estimate until the
   settlement happens, so only an observation received shortly before it -
   within ``max_observation_age`` and naming that settlement as next - says
   what was charged. An earlier one says what was *expected*.
3. **The mark it settled on**, from that same observation.
4. **The size held at that instant**, from the leg's own fills strictly
   before it. A fill at exactly the settlement instant cannot be placed on
   either side of it, so that settlement - and the leg's funding - is
   unmeasured.

Everything is read from observations the replay had already applied when the
leg closed, so no settlement is priced from data that arrived afterwards.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from trading_bot.backtest.loop import SessionFactory
from trading_bot.backtest.market_state import ReplayMarketData, SettlementObservation
from trading_bot.db.models import BacktestFundingPayment
from trading_bot.db.models import Fill as FillRow
from trading_bot.db.models import Order as OrderRow
from trading_bot.db.models import Position as PositionRow
from trading_bot.db.models.enums import Side
from trading_bot.db.scope import RunScope
from trading_bot.exchange.models import MarketRef
from trading_bot.execution.account import PaperAccount
from trading_bot.execution.models import OrderIntent
from trading_bot.portfolio.accounting import FillLot
from trading_bot.portfolio.records import LegRecord


@dataclass(frozen=True, slots=True)
class FundingAttribution:
    #: Signed: positive means the leg received funding. ``None`` = unmeasured.
    amount_usd: Decimal | None
    settlements: int
    #: Why the amount is ``None``; empty when it was measured.
    reason: str | None = None


def attribute_funding(
    *,
    side: Side,
    entries: Sequence[FillLot],
    exits: Sequence[FillLot],
    observations: Mapping[datetime, SettlementObservation],
    max_observation_age: timedelta,
) -> FundingAttribution:
    if not entries or not exits:
        return FundingAttribution(None, 0, "the leg is not flat")
    closed_at = max(lot.filled_at for lot in exits)
    return attribute_funding_until(
        side=side,
        entries=entries,
        exits=exits,
        observations=observations,
        max_observation_age=max_observation_age,
        until=closed_at,
    )


def attribute_funding_until(
    *,
    side: Side,
    entries: Sequence[FillLot],
    exits: Sequence[FillLot],
    observations: Mapping[datetime, SettlementObservation],
    max_observation_age: timedelta,
    until: datetime,
) -> FundingAttribution:
    """Funding settled through ``until``, including a still-open leg.

    This is the same calculation used at close, with a caller-supplied end.
    It lets completeness prove that every settlement crossed by an open leg
    was measured instead of equating "open" with "funding unknown".
    """
    if not entries:
        return FundingAttribution(None, 0, "the leg was never opened")
    opened_at = min(lot.filled_at for lot in entries)
    if until < opened_at:
        return FundingAttribution(None, 0, "the attribution end precedes the entry")
    known = sorted(observations.values(), key=lambda item: item.settles_at)
    intervals = {item.interval_hours for item in known if opened_at <= item.observed_at <= until}
    anchor = next((item for item in reversed(known) if item.observed_at <= until), None)
    if anchor is None or anchor.interval_hours is None:
        return FundingAttribution(None, 0, "no recorded funding schedule for the holding period")
    if len(intervals - {None}) > 1 or None in intervals:
        return FundingAttribution(None, 0, "the funding interval changed or was unpublished")
    interval = timedelta(hours=anchor.interval_hours)
    settlements = _settlements_between(anchor.settles_at, interval, opened_at, until)
    total = Decimal(0)
    sign = Decimal(1) if side is Side.BUY else Decimal(-1)
    for settles_at in settlements:
        if any(lot.filled_at == settles_at for lot in (*entries, *exits)):
            return FundingAttribution(
                None, len(settlements), f"a fill at the settlement instant {settles_at.isoformat()}"
            )
        observation = observations.get(settles_at)
        if observation is None or settles_at - observation.observed_at > max_observation_age:
            return FundingAttribution(
                None,
                len(settlements),
                f"no observation within {max_observation_age} before {settles_at.isoformat()}",
            )
        held = sum((lot.quantity for lot in entries if lot.filled_at < settles_at), Decimal(0))
        held -= sum((lot.quantity for lot in exits if lot.filled_at < settles_at), Decimal(0))
        # A long pays a positive rate and a short receives it.
        total += -sign * held * observation.mark_price * observation.rate
    return FundingAttribution(total, len(settlements))


def settlements_crossed(
    observations: Mapping[datetime, SettlementObservation], opened_at: datetime, until: datetime
) -> int | None:
    """Settlements in ``(opened_at, until]``, or ``None`` if the schedule is unknown.

    For a perpetual leg still open at the end of a run. Funding is booked only
    when a leg closes, so an open leg carries no attributed figure - but if no
    settlement fell inside its holding period, none was charged and its
    funding to date is exactly zero, not unknown.
    """
    known = sorted(observations.values(), key=lambda item: item.settles_at)
    anchor = next((item for item in reversed(known) if item.observed_at <= until), None)
    intervals = {item.interval_hours for item in known if opened_at <= item.observed_at <= until}
    if anchor is None or anchor.interval_hours is None or None in intervals:
        return None
    if len(intervals) > 1:
        return None
    interval = timedelta(hours=anchor.interval_hours)
    return len(_settlements_between(anchor.settles_at, interval, opened_at, until))


def _settlements_between(
    anchor: datetime, interval: timedelta, opened_at: datetime, closed_at: datetime
) -> list[datetime]:
    """Settlement instants in ``(opened_at, closed_at]`` on the anchor's schedule."""
    first = anchor
    if first > opened_at:
        first -= ((first - opened_at) // interval) * interval
        if first <= opened_at:
            first += interval
    else:
        first += ((opened_at - first) // interval + 1) * interval
    instants: list[datetime] = []
    moment = first
    while moment <= closed_at:
        instants.append(moment)
        moment += interval
    return instants


class ReplayFundingAttributor:
    """``PortfolioStore``'s funding source during a backtest."""

    def __init__(
        self,
        market: ReplayMarketData,
        refs_by_market_id: Mapping[int, MarketRef],
        *,
        max_observation_age: timedelta,
    ) -> None:
        self._market = market
        self._refs = dict(refs_by_market_id)
        self._max_age = max_observation_age
        self.measured = 0
        self.unmeasured: dict[str, int] = {}

    async def settled_funding(
        self,
        session: AsyncSession,
        *,
        market_id: int,
        side: Side,
        entries: Sequence[FillLot],
        exits: Sequence[FillLot],
    ) -> Decimal | None:
        ref = self._refs.get(market_id)
        if ref is None:
            return None
        result = attribute_funding(
            side=side,
            entries=entries,
            exits=exits,
            observations=self._market.settlement_observations(ref),
            max_observation_age=self._max_age,
        )
        if result.amount_usd is None:
            reason = result.reason or "unmeasured"
            self.unmeasured[reason] = self.unmeasured.get(reason, 0) + 1
        else:
            self.measured += 1
        return result.amount_usd

    def while_open(self, leg: LegRecord, until: datetime) -> FundingAttribution:
        """Attribute a ``LegRecord`` through a replay instant without closing it."""
        ref = leg.ref
        return attribute_funding_until(
            side=leg.side,
            entries=leg.entries,
            exits=leg.exits,
            observations=self._market.settlement_observations(ref),
            max_observation_age=self._max_age,
            until=until,
        )


class ReplayFundingLedger:
    """Post funding at each virtual settlement, durably and idempotently."""

    def __init__(
        self,
        market: ReplayMarketData,
        refs_by_market_id: Mapping[int, MarketRef],
        session_factory: SessionFactory,
        scope: RunScope,
        account: PaperAccount,
        *,
        max_observation_age: timedelta,
    ) -> None:
        if scope.backtest_run_id is None:
            raise ValueError("a replay funding ledger requires a backtest run")
        self._market = market
        self._refs = dict(refs_by_market_id)
        self._market_ids = {ref: market_id for market_id, ref in refs_by_market_id.items()}
        self._sessions = session_factory
        self._scope = scope
        self._account = account
        self._max_age = max_observation_age
        self._processed: set[tuple[MarketRef, datetime]] = set()
        self.payments_written = 0

    async def settle_due(self, now: datetime) -> None:
        """Post every observed settlement at or before ``now`` exactly once."""
        self._market.catch_up()
        due: list[tuple[datetime, MarketRef, SettlementObservation]] = []
        for ref in self._market.refs:
            for settles_at, observation in self._market.settlement_observations(ref).items():
                key = (ref, settles_at)
                if settles_at <= now and key not in self._processed:
                    due.append((settles_at, ref, observation))
        for settles_at, ref, observation in sorted(due, key=lambda item: (item[0], str(item[1]))):
            await self._settle(ref, observation)
            self._processed.add((ref, settles_at))

    async def _settle(self, ref: MarketRef, observation: SettlementObservation) -> None:
        if observation.settles_at - observation.observed_at > self._max_age:
            return
        market_id = self._market_ids.get(ref)
        if market_id is None:
            return
        at = observation.settles_at
        async with self._sessions() as session:
            positions = (
                await session.execute(
                    select(PositionRow.id, PositionRow.side)
                    .where(
                        *self._scope.filters(PositionRow.mode, PositionRow.backtest_run_id),
                        PositionRow.market_id == market_id,
                        PositionRow.is_shadow.is_(False),
                        PositionRow.opened_at < at,
                        or_(PositionRow.closed_at.is_(None), PositionRow.closed_at >= at),
                    )
                    .order_by(PositionRow.id)
                )
            ).all()
            if not positions:
                return
            position_ids = [row.id for row in positions]
            fills = (
                await session.execute(
                    select(
                        FillRow.position_id,
                        OrderRow.intent,
                        FillRow.quantity,
                        FillRow.filled_at,
                    )
                    .join(OrderRow, OrderRow.id == FillRow.order_id)
                    .where(
                        FillRow.position_id.in_(position_ids),
                        *self._scope.filters(FillRow.mode, FillRow.backtest_run_id),
                        FillRow.filled_at <= at,
                    )
                    .order_by(FillRow.filled_at, FillRow.id)
                )
            ).all()
            by_position: dict[int, list[Any]] = {}
            for fill in fills:
                if fill.position_id is not None:
                    by_position.setdefault(int(fill.position_id), []).append(fill)

            pending: list[tuple[int, Decimal]] = []
            for position in positions:
                lots = by_position.get(position.id, [])
                # A venue ordering cannot be inferred when a fill and funding
                # share the same timestamp, so do not invent a payment.
                if any(fill.filled_at == at for fill in lots):
                    continue
                opened = sum(
                    (
                        fill.quantity
                        for fill in lots
                        if fill.intent == OrderIntent.OPEN.value and fill.filled_at < at
                    ),
                    Decimal(0),
                )
                closed = sum(
                    (
                        fill.quantity
                        for fill in lots
                        if fill.intent == OrderIntent.CLOSE.value and fill.filled_at < at
                    ),
                    Decimal(0),
                )
                held = opened - closed
                if held <= 0:
                    continue
                sign = Decimal(1) if position.side is Side.BUY else Decimal(-1)
                amount = -sign * held * observation.mark_price * observation.rate
                statement = (
                    insert(BacktestFundingPayment)
                    .values(
                        backtest_run_id=self._scope.backtest_run_id,
                        position_id=position.id,
                        market_id=market_id,
                        settled_at=at,
                        observed_at=observation.observed_at,
                        quantity=held,
                        rate=observation.rate,
                        mark_price=observation.mark_price,
                        amount_usd=amount,
                    )
                    .on_conflict_do_nothing(constraint="run_position_funding_settlement")
                    .returning(BacktestFundingPayment.amount_usd)
                )
                inserted = await session.scalar(statement)
                if inserted is not None:
                    pending.append((position.id, inserted))

        # The transaction committed before the in-memory account moves. A
        # commit error fails the run; a duplicate retry returns no row and
        # therefore cannot charge the account twice.
        for _, amount in pending:
            await self._account.apply_funding(amount)
            self.payments_written += 1
