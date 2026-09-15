"""What one backtest run measured - read from its own rows, and nothing else.

Every query is scoped to exactly one run (``backtest_run_id``). The report
never adds paper or live results, never another run's, and keeps what the
strategy *expected* (theoretical opportunity edges) apart from what the
replayed execution *got*.

Presentation rules that carry the accounting's honesty:

- **Totals are totals.** Execution fees are every non-shadow fill's fee -
  open positions, partial fills, unpaired legs and completed trades alike -
  split by the wallet that paid them. Fees charged to *completed* trades are
  a separate, smaller figure, and so is their slippage. Slippage is an
  attribution already inside fill prices; it is never subtracted again.
- **Trade statistics are over completed paired trades only.** An open
  attempt, a one-legged fill or a partially closed pair is not a win or a
  loss yet, and is counted apart.
- **The equity change is reconciled.** ``initial cash + price P&L + funding
  + unrealised - all execution fees`` must equal the final equity; the
  difference is reported, not assumed away.
- **A derived metric carries its caveat.** Unless the run is rankable
  (``COMPLETED``), return, drawdown, Sharpe/Sortino and every trade ratio are
  printed with the reason they are not a result, and the JSON carries the
  same ``caveat``. Unavailable is ``None``, never zero.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select

from trading_bot.backtest import report_data as data
from trading_bot.backtest.completeness import component_names
from trading_bot.backtest.loop import SessionFactory
from trading_bot.db.models import BacktestRun
from trading_bot.db.models.enums import BacktestRunStatus, ExecutionMode
from trading_bot.db.scope import RunScope
from trading_bot.portfolio.accounting import (
    max_drawdown,
    sample_returns,
    sharpe_ratio,
    sortino_ratio,
    summarise_trades,
)
from trading_bot.portfolio.snapshots import floor_to
from trading_bot.portfolio.store import PortfolioStore

_FAR_FUTURE = timedelta(days=36_500)
ZERO = Decimal(0)


@dataclass(slots=True)
class BacktestReport:
    run_uid: str
    status: str
    dataset_source: str
    requested: tuple[str, str]
    actual: tuple[str | None, str | None]
    markets: list[str]
    config_hash: str
    code_revision: str | None
    code_dirty: bool | None
    code_worktree_hash: str | None
    failure_reason: str | None
    # --- verdict -------------------------------------------------------
    performance_rankable: bool
    caveat: str | None
    incomplete_components: list[str]
    fidelity_limitations: list[str]
    completeness: dict[str, Any] | None
    # --- money ---------------------------------------------------------
    initial_cash_usd: Decimal
    final_equity_usd: Decimal | None
    valuation_status: str | None
    valuation_captured_at: str | None
    total_return_pct: float | None
    realized_pnl_usd: Decimal
    realized_pnl_all_legs_usd: Decimal
    unrealized_pnl_usd: Decimal | None
    execution_fees_usd: Decimal
    fees_paid_in_cash_usd: Decimal
    fees_paid_in_bnb_usd: Decimal
    completed_trade_fees_usd: Decimal
    slippage_attribution_usd: Decimal
    completed_trade_slippage_usd: Decimal
    funding_usd: Decimal | None
    borrow_usd: Decimal | None
    unmeasured_components: list[str]
    pnl_complete: bool
    expected_final_equity_usd: Decimal | None
    equity_reconciliation_difference_usd: Decimal | None
    # --- completed trades ----------------------------------------------
    trade_count: int
    winning_trades: int
    losing_trades: int
    breakeven_trades: int
    gross_profit_usd: Decimal
    gross_loss_usd: Decimal
    average_trade_usd: Decimal | None
    average_win_usd: Decimal | None
    average_loss_usd: Decimal | None
    win_rate: float | None
    profit_factor: float | None
    expectancy_usd: Decimal | None
    by_strategy: dict[str, dict[str, Any]]
    open_attempts: int
    unpaired_open_attempts: int
    # --- exposure ------------------------------------------------------
    exposure_time_pct: float
    turnover_usd: Decimal
    turnover_ratio: float | None
    peak_gross_exposure_usd: Decimal | None
    return_on_peak_exposure_pct: float | None
    worst_unhedged_notional_usd: Decimal | None
    # --- curve ---------------------------------------------------------
    max_drawdown_usd: Decimal | None
    sharpe_ratio: float | None
    sortino_ratio: float | None
    return_interval_seconds: int
    return_observations: int
    equity_points: int
    # --- what stopped trades / what was expected -----------------------
    opportunities_by_status: dict[str, int]
    rejection_reasons: dict[str, int]
    theoretical_net_edge_bps: dict[str, float | None]
    risk_refusals: dict[str, int]
    order_statuses: dict[str, int]
    order_rejections: dict[str, int]
    attempts: dict[str, int]
    # --- execution quality ---------------------------------------------
    latency_ms: dict[str, float | None]
    slippage_bps: dict[str, float | None]
    fills: int
    fills_on_book_after_signal: int
    fills_on_book_not_after_signal: int
    maker_fills: int
    # --- data ----------------------------------------------------------
    initialization_events: int
    events_accepted: int
    events_rejected: int
    events_replayed: int
    dataset_fingerprint: str | None
    dataset_issues: dict[str, Any] | None
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        document: dict[str, Any] = _jsonable(asdict(self))
        return document


def _caveat(run: BacktestRun, rankable: bool, incomplete: list[str]) -> str | None:
    if rankable:
        return None
    if run.status in (BacktestRunStatus.FAILED, BacktestRunStatus.CANCELLED):
        return f"run {run.status.value}: its rows are partial and describe no result"
    if run.status in (BacktestRunStatus.PENDING, BacktestRunStatus.RUNNING):
        return f"run {run.status.value}: not finished"
    return "INCOMPLETE " + ", ".join(incomplete or ["run"]) + ": not a rankable result"


async def build_report(session_factory: SessionFactory, run_uid: uuid.UUID) -> BacktestReport:
    async with session_factory() as session:
        run = (
            await session.execute(select(BacktestRun).where(BacktestRun.run_uid == run_uid))
        ).scalar_one_or_none()
    if run is None:
        raise LookupError(f"no backtest run {run_uid}")
    scope = RunScope.backtest(run.id)
    snapshot = run.config_snapshot
    venue = str(snapshot.get("exchange", {}).get("venue", "binance"))
    interval = timedelta(milliseconds=int(snapshot["portfolio"]["snapshot_interval_ms"]))
    minimum = int(snapshot["portfolio"]["min_return_observations"])
    initial_cash = Decimal(str(snapshot["execution"]["paper_cash_usd"]))
    fees_in_bnb = bool(snapshot.get("costs", {}).get("pay_fees_in_bnb", False))
    annual_pct = float(snapshot["portfolio"].get("risk_free_rate_annual_pct", 0.0))
    risk_free = annual_pct / 100 / (timedelta(days=365) / interval) if annual_pct > 0 else 0.0

    store = PortfolioStore(session_factory, mode=ExecutionMode.BACKTEST, backtest_run_id=run.id)
    completed = await store.attempts_closed_between(venue, None, run.requested_end + _FAR_FUTURE)
    live = await store.live_attempts(venue)
    trades = [attempt.paired() for attempt in completed]
    everything = trades + [attempt.paired() for attempt in live]
    statistics = summarise_trades([trade.realized_pnl_usd for trade in trades])
    unmeasured: list[str] = []
    for trade in everything:
        for component in trade.unmeasured:
            if component not in unmeasured:
                unmeasured.append(component)
    legs = [leg for trade in everything for leg in trade.legs]

    async with session_factory() as session:
        curve = await data.equity_curve(session, scope)
        latest = await data.latest_snapshot(session, scope)
        peak = await data.peak_gross_exposure(session, scope)
        opportunities = await data.opportunities(session, scope)
        refusals = await data.risk_refusals(session, scope)
        orders = await data.orders(session, scope)
        fills = await data.fills(session, scope, fees_in_bnb=fees_in_bnb)

    completeness = run.completeness
    accounting = (completeness or {}).get("accounting")
    if accounting is not None:
        # The run's verdict knows what the rows alone cannot: that an open
        # perpetual leg crossed no settlement, so its funding is zero.
        unmeasured = list(accounting["unmeasured"])
    funding_measured = bool(legs) and "funding" not in unmeasured
    rankable = bool(completeness and completeness.get("performance_rankable"))
    incomplete = component_names(completeness)
    # A terminal valuation between grid points is not a period return.
    sample = sample_returns(
        [point for point in curve if floor_to(point[0], interval) == point[0]], interval=interval
    )
    final_equity = latest.equity_usd if latest is not None else None
    unrealized = latest.unrealized_pnl_usd if latest is not None else None
    price_pnl = sum((leg.price_pnl_usd for leg in legs), ZERO)
    funding_total = sum((leg.funding_pnl_usd or ZERO for leg in legs), ZERO)
    expected_equity = (
        initial_cash
        + price_pnl
        + funding_total
        + unrealized
        - fills.fees_cash_usd
        - fills.fees_bnb_usd
        if unrealized is not None
        else None
    )
    change = final_equity - initial_cash if final_equity is not None else None
    return BacktestReport(
        run_uid=str(run.run_uid),
        status=run.status.value,
        dataset_source=run.dataset_source,
        requested=(run.requested_start.isoformat(), run.requested_end.isoformat()),
        actual=(_iso(run.actual_start), _iso(run.actual_end)),
        markets=list(run.markets),
        config_hash=run.config_hash,
        code_revision=run.code_revision,
        code_dirty=run.code_dirty,
        code_worktree_hash=run.code_worktree_hash,
        failure_reason=run.failure_reason,
        performance_rankable=rankable,
        caveat=_caveat(run, rankable, incomplete),
        incomplete_components=incomplete,
        fidelity_limitations=list(
            (completeness or {}).get("execution_model", {}).get("limitations", [])
        ),
        completeness=completeness,
        initial_cash_usd=initial_cash,
        final_equity_usd=final_equity,
        valuation_status=latest.valuation_status.value if latest is not None else None,
        valuation_captured_at=latest.captured_at.isoformat() if latest is not None else None,
        total_return_pct=(
            float(change / initial_cash * 100) if change is not None and initial_cash > 0 else None
        ),
        realized_pnl_usd=statistics.net_pnl_usd,
        realized_pnl_all_legs_usd=sum((leg.realized_pnl_usd for leg in legs), ZERO),
        unrealized_pnl_usd=unrealized,
        execution_fees_usd=fills.fees_usd,
        fees_paid_in_cash_usd=fills.fees_cash_usd,
        fees_paid_in_bnb_usd=fills.fees_bnb_usd,
        completed_trade_fees_usd=sum((trade.fees_usd for trade in trades), ZERO),
        slippage_attribution_usd=sum((leg.slippage_usd for leg in legs), ZERO),
        completed_trade_slippage_usd=sum((trade.slippage_usd for trade in trades), ZERO),
        funding_usd=funding_total if funding_measured else None,
        borrow_usd=None,
        unmeasured_components=unmeasured,
        pnl_complete=not unmeasured,
        expected_final_equity_usd=expected_equity,
        equity_reconciliation_difference_usd=(
            final_equity - expected_equity
            if final_equity is not None and expected_equity is not None
            else None
        ),
        trade_count=statistics.trade_count,
        winning_trades=statistics.winning_trades,
        losing_trades=statistics.losing_trades,
        breakeven_trades=statistics.breakeven_trades,
        gross_profit_usd=statistics.gross_profit_usd,
        gross_loss_usd=statistics.gross_loss_usd,
        average_trade_usd=statistics.average_trade_usd,
        average_win_usd=statistics.average_win_usd,
        average_loss_usd=statistics.average_loss_usd,
        win_rate=statistics.win_rate,
        profit_factor=statistics.profit_factor,
        expectancy_usd=statistics.expectancy_usd,
        by_strategy=data.by_strategy(trades),
        open_attempts=len(live),
        unpaired_open_attempts=sum(attempt.is_unpaired for attempt in live),
        exposure_time_pct=data.exposure_time_pct(
            everything, run.requested_start, run.requested_end
        ),
        turnover_usd=fills.turnover_usd,
        turnover_ratio=float(fills.turnover_usd / initial_cash) if initial_cash > 0 else None,
        peak_gross_exposure_usd=peak,
        return_on_peak_exposure_pct=(
            float(change / peak * 100) if change is not None and peak else None
        ),
        worst_unhedged_notional_usd=orders.worst_unhedged_notional_usd,
        max_drawdown_usd=max_drawdown([value for _, value in curve]),
        sharpe_ratio=(
            sharpe_ratio(sample, risk_free_per_period=risk_free, minimum=minimum)
            if sample is not None
            else None
        ),
        sortino_ratio=(
            sortino_ratio(sample, risk_free_per_period=risk_free, minimum=minimum)
            if sample is not None
            else None
        ),
        return_interval_seconds=int(interval.total_seconds()),
        return_observations=len(sample.returns) if sample is not None else 0,
        equity_points=len(curve),
        opportunities_by_status=opportunities["statuses"],
        rejection_reasons=opportunities["reasons"],
        theoretical_net_edge_bps=opportunities["edge"],
        risk_refusals=refusals,
        order_statuses=orders.statuses,
        order_rejections=orders.rejections,
        attempts=orders.attempts,
        latency_ms=orders.latency,
        slippage_bps=fills.slippage_bps,
        fills=fills.count,
        fills_on_book_after_signal=fills.on_book_after_signal,
        fills_on_book_not_after_signal=fills.on_book_not_after_signal,
        maker_fills=fills.maker,
        initialization_events=run.initialization_events,
        events_accepted=run.events_accepted,
        events_rejected=run.events_rejected,
        events_replayed=run.events_replayed,
        dataset_fingerprint=run.dataset_fingerprint,
        dataset_issues=run.dataset_issues,
        warnings=list(run.warnings or []),
    )


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, tuple | list):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    return value
