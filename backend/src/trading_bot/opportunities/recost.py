"""Re-pricing stored opportunities under a different fee schedule.

Phase 7 recorded opportunities before the real cost model existed, on the
argument that the prices behind each decision are on the row - so fees can be
re-derived later, while a day that was never recorded is gone for good. This
is that argument made good.

**Nothing is rewritten.** A stored row is what the strategy believed at the
time, and overwriting it would destroy the only record of that. Re-costing
produces a report.

What can and cannot be re-derived:

- **Fees: yes.** They are a rate on notional, and the notional is on the row.
- **Slippage: no.** It came from walking an order book, and retention deletes
  those after three days. The stored figure is carried through unchanged.
- **Funding: no.** It depends on which settlements the holding period crossed,
  which needs the schedule as it was at that moment. Carried through unchanged.

So a re-cost answers one question precisely: does a different fee schedule
change any verdict? For this strategy that is the question that matters, since
fees are the largest single cost.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from trading_bot.db.models import Market, Opportunity
from trading_bot.db.models.enums import MarketType
from trading_bot.exchange.models import BPS_SCALE
from trading_bot.strategy.fees import FeeSchedule, OrderRole


@dataclass(frozen=True, slots=True)
class RecostedOpportunity:
    """One stored row, as it was and as a different fee schedule would price it."""

    opportunity_id: int
    strategy: str
    symbol: str
    notional_usd: Decimal
    gross_edge_bps: Decimal
    stored_fees_usd: Decimal
    recosted_fees_usd: Decimal
    stored_net_bps: Decimal
    recosted_net_bps: Decimal

    @property
    def fee_saving_usd(self) -> Decimal:
        return self.stored_fees_usd - self.recosted_fees_usd

    @property
    def was_profitable(self) -> bool:
        return self.stored_net_bps > 0

    @property
    def is_profitable(self) -> bool:
        return self.recosted_net_bps > 0

    @property
    def flipped(self) -> bool:
        """True when the new schedule changes the verdict either way."""
        return self.was_profitable != self.is_profitable


@dataclass(frozen=True, slots=True)
class RecostReport:
    rows: tuple[RecostedOpportunity, ...]
    schedule: str

    @property
    def total(self) -> int:
        return len(self.rows)

    @property
    def was_profitable(self) -> int:
        return sum(row.was_profitable for row in self.rows)

    @property
    def is_profitable(self) -> int:
        return sum(row.is_profitable for row in self.rows)

    @property
    def newly_profitable(self) -> tuple[RecostedOpportunity, ...]:
        return tuple(row for row in self.rows if row.flipped and row.is_profitable)

    def mean(self, attribute: str) -> Decimal | None:
        if not self.rows:
            return None
        values = [getattr(row, attribute) for row in self.rows]
        return sum(values, Decimal(0)) / len(values)

    def describe(self) -> str:
        if not self.rows:
            return "no stored opportunities to re-cost"
        stored = self.mean("stored_net_bps")
        recosted = self.mean("recosted_net_bps")
        lines = [
            f"re-costed {self.total} stored opportunities under: {self.schedule}",
            f"  mean net edge  {stored:+.2f} bps -> {recosted:+.2f} bps",
            f"  profitable     {self.was_profitable} -> {self.is_profitable}",
        ]
        newly = self.newly_profitable
        if newly:
            best = max(newly, key=lambda row: row.recosted_net_bps)
            lines.append(
                f"  {len(newly)} opportunities newly clear costs, best "
                f"{best.symbol} at {best.recosted_net_bps:+.2f} bps"
            )
        else:
            lines.append("  no verdict changed: the fee schedule is not what decides this")
        lines.append(
            "  slippage and funding are carried through unchanged - "
            "neither can be re-derived from a stored row"
        )
        return "\n".join(lines)


def recost_row(
    opportunity: Opportunity,
    symbol: str,
    market_types: Sequence[MarketType],
    schedule: FeeSchedule,
    *,
    entry_role: OrderRole,
    exit_role: OrderRole,
) -> RecostedOpportunity:
    """Re-derive fees for one row; every other cost is carried through.

    Both legs traded the same notional, so the fee is that notional at each
    leg's rate, charged on entry and on exit.
    """
    notional = opportunity.notional_usd
    fees = Decimal(0)
    for market_type in market_types:
        instrument = schedule.spot if market_type is MarketType.SPOT else schedule.perpetual
        rate = instrument.rate(entry_role) + instrument.rate(exit_role)
        fees += notional * rate / BPS_SCALE
    other_costs = (
        opportunity.estimated_slippage_usd
        + opportunity.funding_cost_usd
        + opportunity.borrow_cost_usd
        + opportunity.other_costs_usd
        + opportunity.safety_buffer_usd
    )
    recosted_net_usd = opportunity.gross_edge_usd - fees - other_costs
    return RecostedOpportunity(
        opportunity_id=opportunity.id,
        strategy=opportunity.strategy,
        symbol=symbol,
        notional_usd=notional,
        gross_edge_bps=opportunity.gross_edge_bps,
        stored_fees_usd=opportunity.estimated_fees_usd,
        recosted_fees_usd=fees,
        stored_net_bps=opportunity.net_edge_bps,
        recosted_net_bps=(recosted_net_usd / notional * BPS_SCALE if notional > 0 else Decimal(0)),
    )


async def recost(
    session: AsyncSession,
    schedule: FeeSchedule,
    *,
    entry_role: OrderRole = OrderRole.TAKER,
    exit_role: OrderRole = OrderRole.TAKER,
    limit: int | None = None,
) -> RecostReport:
    """Re-cost every stored opportunity, newest first."""
    primary = select(Market.id, Market.symbol, Market.market_type).subquery()
    statement = (
        select(Opportunity, primary.c.symbol, primary.c.market_type)
        .join(primary, primary.c.id == Opportunity.market_id)
        .order_by(Opportunity.detected_at.desc())
    )
    if limit is not None:
        statement = statement.limit(limit)
    result = await session.execute(statement)
    rows = [
        recost_row(
            opportunity,
            symbol,
            # Both legs of a spot/perp basis: the stored leg and its opposite.
            (market_type, _other(market_type)),
            schedule,
            entry_role=entry_role,
            exit_role=exit_role,
        )
        for opportunity, symbol, market_type in result.all()
    ]
    return RecostReport(rows=tuple(rows), schedule=schedule.describe())


def _other(market_type: MarketType) -> MarketType:
    """A basis trade's other leg is the opposite instrument class."""
    return MarketType.PERPETUAL if market_type is MarketType.SPOT else MarketType.SPOT
