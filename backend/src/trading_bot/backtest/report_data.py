"""The run-scoped reads and pure computations behind a backtest report.

Every query filters on one run. Every figure is computed from durable rows,
never from the replay's in-memory state, so a report printed a week later
from another process says exactly what one printed at the end of the run.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from statistics import median
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from trading_bot.db.models import Fill, Opportunity, Order, PortfolioSnapshot, RiskEvent
from trading_bot.db.models.enums import RiskDecision, Side
from trading_bot.db.scope import RunScope
from trading_bot.portfolio.accounting import PairedTrade, summarise_trades

ZERO = Decimal(0)


async def equity_curve(session: AsyncSession, scope: RunScope) -> list[tuple[datetime, Decimal]]:
    rows = await session.execute(
        select(PortfolioSnapshot.captured_at, PortfolioSnapshot.equity_usd)
        .where(
            *scope.filters(PortfolioSnapshot.mode, PortfolioSnapshot.backtest_run_id),
            PortfolioSnapshot.equity_usd.is_not(None),
        )
        .order_by(PortfolioSnapshot.captured_at)
    )
    return [(at, value) for at, value in rows if value is not None]


async def latest_snapshot(session: AsyncSession, scope: RunScope) -> PortfolioSnapshot | None:
    return (
        await session.execute(
            select(PortfolioSnapshot)
            .where(*scope.filters(PortfolioSnapshot.mode, PortfolioSnapshot.backtest_run_id))
            .order_by(PortfolioSnapshot.captured_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def peak_gross_exposure(session: AsyncSession, scope: RunScope) -> Decimal | None:
    peak: Decimal | None = await session.scalar(
        select(func.max(PortfolioSnapshot.gross_exposure_usd)).where(
            *scope.filters(PortfolioSnapshot.mode, PortfolioSnapshot.backtest_run_id)
        )
    )
    return peak


async def opportunities(session: AsyncSession, scope: RunScope) -> dict[str, Any]:
    rows = await session.execute(
        select(Opportunity.status, Opportunity.rejection_reason, Opportunity.net_edge_bps).where(
            *scope.filters(Opportunity.mode, Opportunity.backtest_run_id)
        )
    )
    statuses: Counter[str] = Counter()
    reasons: Counter[str] = Counter()
    edges: list[float] = []
    for status, reason, edge in rows:
        statuses[status.value] += 1
        for part in (reason or "").split(","):
            if part.strip():
                reasons[part.strip()] += 1
        if edge is not None:
            edges.append(float(edge))
    return {
        "statuses": dict(sorted(statuses.items())),
        "reasons": dict(reasons.most_common()),
        "edge": distribution(edges),
    }


async def risk_refusals(session: AsyncSession, scope: RunScope) -> dict[str, int]:
    rows = await session.execute(
        select(RiskEvent.decision, RiskEvent.event_type).where(
            *scope.filters(RiskEvent.mode, RiskEvent.backtest_run_id),
            RiskEvent.decision != RiskDecision.APPROVED,
        )
    )
    counts = Counter(f"{decision.value}:{event_type.value}" for decision, event_type in rows)
    return dict(counts.most_common())


@dataclass(frozen=True, slots=True)
class EntryLegs:
    """What the entry orders of each attempt filled, per side."""

    statuses: dict[str, int]
    rejections: dict[str, int]
    attempts: dict[str, int]
    latency: dict[str, float | None]
    worst_unhedged_notional_usd: Decimal | None


async def orders(session: AsyncSession, scope: RunScope) -> EntryLegs:
    rows = (
        await session.execute(
            select(
                Order.status,
                Order.evidence,
                Order.latency_ms,
                Order.attempt_id,
                Order.intent,
                Order.side,
                Order.filled_quantity,
                Order.average_fill_price,
                Order.is_shadow,
            ).where(*scope.filters(Order.mode, Order.backtest_run_id))
        )
    ).all()
    statuses = Counter(row.status.value for row in rows)
    rejections = Counter(
        str((row.evidence or {}).get("rejection_code"))
        for row in rows
        if (row.evidence or {}).get("rejection_code")
    )
    legs: dict[str, list[Any]] = {}
    for row in rows:
        if row.intent == "OPEN" and row.attempt_id and not row.is_shadow:
            legs.setdefault(row.attempt_id, []).append(row)
    attempts: Counter[str] = Counter()
    worst: Decimal | None = None
    for filled in legs.values():
        bought = sum((leg.filled_quantity for leg in filled if leg.side is Side.BUY), ZERO)
        sold = sum((leg.filled_quantity for leg in filled if leg.side is Side.SELL), ZERO)
        if bought == 0 and sold == 0:
            attempts["nothing_filled"] += 1
            continue
        if len(filled) == 2 and bought == sold:
            attempts["hedged"] += 1
            continue
        attempts["unhedged"] += 1
        heavier = max(filled, key=lambda leg: leg.filled_quantity)
        naked = abs(bought - sold) * (heavier.average_fill_price or ZERO)
        worst = naked if worst is None else max(worst, naked)
    return EntryLegs(
        statuses=dict(sorted(statuses.items())),
        rejections=dict(rejections.most_common()),
        attempts=dict(sorted(attempts.items())),
        latency=distribution([float(row.latency_ms) for row in rows if row.latency_ms is not None]),
        worst_unhedged_notional_usd=worst,
    )


@dataclass(frozen=True, slots=True)
class FillFacts:
    fees_cash_usd: Decimal
    fees_bnb_usd: Decimal
    turnover_usd: Decimal
    slippage_bps: dict[str, float | None]
    on_book_after_signal: int
    on_book_not_after_signal: int
    maker: int
    count: int

    @property
    def fees_usd(self) -> Decimal:
        return self.fees_cash_usd + self.fees_bnb_usd


async def fills(session: AsyncSession, scope: RunScope, *, fees_in_bnb: bool) -> FillFacts:
    """Every non-shadow fill of the run - open, partial, closed or unpaired alike."""
    rows = (
        await session.execute(
            select(
                Fill.price,
                Fill.quantity,
                Fill.fee_usd,
                Fill.fee_asset,
                Fill.slippage_bps,
                Fill.is_maker,
                Fill.book_local_timestamp,
                Order.evidence,
            )
            .join(Order, Order.id == Fill.order_id)
            .where(*scope.filters(Fill.mode, Fill.backtest_run_id), Order.is_shadow.is_(False))
        )
    ).all()
    after = not_after = 0
    cash = bnb = turnover = ZERO
    for row in rows:
        turnover += row.price * row.quantity
        # The fill's recorded asset decides, as it does for durable cash; the
        # configuration only decides for rows that never recorded one.
        paid_in_bnb = row.fee_asset == "BNB" or (row.fee_asset is None and fees_in_bnb)
        if paid_in_bnb:
            bnb += row.fee_usd
        else:
            cash += row.fee_usd
        generated = (row.evidence or {}).get("signal_generated_at")
        if generated is None or row.book_local_timestamp is None:
            continue
        if row.book_local_timestamp > datetime.fromisoformat(generated):
            after += 1
        else:
            not_after += 1
    return FillFacts(
        fees_cash_usd=cash,
        fees_bnb_usd=bnb,
        turnover_usd=turnover,
        slippage_bps=distribution(
            [float(row.slippage_bps) for row in rows if row.slippage_bps is not None]
        ),
        on_book_after_signal=after,
        on_book_not_after_signal=not_after,
        maker=sum(bool(row.is_maker) for row in rows),
        count=len(rows),
    )


def exposure_time_pct(trades: Sequence[PairedTrade], start: datetime, end: datetime) -> float:
    """Share of the requested range during which any position was open."""
    intervals = sorted(
        (max(trade.opened_at, start), min(trade.closed_at or end, end))
        for trade in trades
        if trade.opened_at is not None and trade.opened_at < end
    )
    covered = timedelta(0)
    current: tuple[datetime, datetime] | None = None
    for begin, finish in intervals:
        if finish <= begin:
            continue
        if current is None or begin > current[1]:
            if current is not None:
                covered += current[1] - current[0]
            current = (begin, finish)
        else:
            current = (current[0], max(current[1], finish))
    if current is not None:
        covered += current[1] - current[0]
    return 100 * covered / (end - start)


def by_strategy(trades: Sequence[PairedTrade]) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[Decimal]] = {}
    for trade in trades:
        grouped.setdefault(trade.strategy, []).append(trade.realized_pnl_usd)
    result: dict[str, dict[str, Any]] = {}
    for strategy, values in sorted(grouped.items()):
        stats = summarise_trades(values)
        result[strategy] = {
            "trades": stats.trade_count,
            "net_pnl_usd": stats.net_pnl_usd,
            "win_rate": stats.win_rate,
            "profit_factor": stats.profit_factor,
        }
    return result


def distribution(values: list[float]) -> dict[str, float | None]:
    if not values:
        return {"count": 0, "mean": None, "p50": None, "p95": None, "max": None}
    ordered = sorted(values)
    return {
        "count": float(len(ordered)),
        "mean": sum(ordered) / len(ordered),
        "p50": median(ordered),
        "p95": ordered[min(len(ordered) - 1, round(0.95 * (len(ordered) - 1)))],
        "max": ordered[-1],
    }
