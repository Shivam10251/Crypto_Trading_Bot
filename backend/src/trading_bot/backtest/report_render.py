"""The text form of a backtest report: the same facts and caveats as its JSON."""

from __future__ import annotations

from decimal import Decimal

from trading_bot.backtest.report import BacktestReport


def _fmt(value: object, suffix: str = "") -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float | Decimal):
        return f"{value:,.4f}{suffix}"
    return f"{value}{suffix}"


def render(report: BacktestReport) -> str:
    pnl_label = "" if report.pnl_complete else "  [P&L INCOMPLETE]"
    derived = f"  [{report.caveat}]" if report.caveat else ""
    lines = [
        f"Backtest {report.run_uid}  status={report.status}  "
        f"rankable={'yes' if report.performance_rankable else 'NO'}",
        f"  data      {report.dataset_source}  requested {report.requested[0]} -> "
        f"{report.requested[1]}  replayed {report.actual[0]} -> {report.actual[1]}",
        f"  markets   {', '.join(report.markets)}",
        f"  config    {report.config_hash[:16]}  code {report.code_revision or 'unknown'}"
        f"{' (dirty)' if report.code_dirty else ''}",
        f"  source digest          {report.code_worktree_hash or 'unknown'}",
    ]
    if report.failure_reason:
        lines.append(f"  failure   {report.failure_reason}")
    if report.caveat:
        lines.append(f"  !! {report.caveat}")
    if not report.pnl_complete:
        lines.append(
            "  !! P&L EXCLUDES: "
            + ", ".join(report.unmeasured_components)
            + " - it is not a total and must not be ranked as one"
        )
    lines += ["", "Execution model (declared fidelity, applies to every figure)"]
    lines += [f"  - {limitation}" for limitation in report.fidelity_limitations] or ["  n/a"]
    lines += [
        "",
        "Money",
        f"  initial cash            {_fmt(report.initial_cash_usd)}",
        f"  final equity            {_fmt(report.final_equity_usd)}  "
        f"(valuation {report.valuation_status or 'n/a'} at {report.valuation_captured_at})",
        f"  total return            {_fmt(report.total_return_pct, ' %')}{derived}",
        f"  realised, completed     {_fmt(report.realized_pnl_usd)}{pnl_label}",
        f"  realised, all legs      {_fmt(report.realized_pnl_all_legs_usd)}{pnl_label}",
        f"  unrealised              {_fmt(report.unrealized_pnl_usd)}",
        f"  execution fees (all)    {_fmt(report.execution_fees_usd)}  "
        f"(cash {_fmt(report.fees_paid_in_cash_usd)}, BNB {_fmt(report.fees_paid_in_bnb_usd)})",
        f"  fees, completed trades  {_fmt(report.completed_trade_fees_usd)}",
        f"  slippage (attribution)  {_fmt(report.slippage_attribution_usd)}  "
        f"completed trades {_fmt(report.completed_trade_slippage_usd)} - inside fill prices",
        f"  funding                 {_fmt(report.funding_usd)}",
        f"  spot borrow             {_fmt(report.borrow_usd)}",
        f"  equity reconciliation   expected {_fmt(report.expected_final_equity_usd)}  "
        f"difference {_fmt(report.equity_reconciliation_difference_usd)}",
        "",
        "Completed paired trades",
        f"  count                   {report.trade_count}  (won {report.winning_trades}, "
        f"lost {report.losing_trades}, breakeven {report.breakeven_trades})",
        f"  gross profit / loss     {_fmt(report.gross_profit_usd)} / "
        f"{_fmt(report.gross_loss_usd)}{pnl_label}",
        f"  average trade           {_fmt(report.average_trade_usd)}  "
        f"win {_fmt(report.average_win_usd)}  loss {_fmt(report.average_loss_usd)}{pnl_label}",
        f"  win rate                {_fmt(report.win_rate)}{pnl_label}{derived}",
        f"  profit factor           {_fmt(report.profit_factor)}{pnl_label}{derived}",
        f"  expectancy              {_fmt(report.expectancy_usd)}{pnl_label}{derived}",
        f"  by strategy             {report.by_strategy}",
        f"  open attempts           {report.open_attempts}  "
        f"(unpaired {report.unpaired_open_attempts})",
        f"  entry attempts          {report.attempts or {}}",
        "",
        "Exposure",
        f"  time exposed            {_fmt(report.exposure_time_pct, ' %')}",
        f"  turnover                {_fmt(report.turnover_usd)}  "
        f"({_fmt(report.turnover_ratio)} x initial cash)",
        f"  peak gross (sampled)    {_fmt(report.peak_gross_exposure_usd)}",
        f"  return on peak gross    {_fmt(report.return_on_peak_exposure_pct, ' %')}{derived}",
        f"  worst unhedged entry    {_fmt(report.worst_unhedged_notional_usd)}",
        "",
        "Equity curve",
        f"  max drawdown            {_fmt(report.max_drawdown_usd)}{derived}",
        f"  Sharpe / Sortino        {_fmt(report.sharpe_ratio)} / {_fmt(report.sortino_ratio)}  "
        f"annualised from {report.return_observations} returns at "
        f"{report.return_interval_seconds} s{derived}",
        "",
        "Theoretical (what the strategy expected, not what executed)",
        f"  opportunities           {report.opportunities_by_status}",
        f"  net edge bps            {report.theoretical_net_edge_bps}",
        "",
        "Why trades did not happen",
        f"  rejection reasons       {report.rejection_reasons}",
        f"  risk refusals           {report.risk_refusals}",
        f"  order statuses          {report.order_statuses}",
        f"  order rejections        {report.order_rejections}",
        "",
        "Execution quality",
        f"  order latency ms        {report.latency_ms}",
        f"  fill slippage bps       {report.slippage_bps}",
        f"  fills                   {report.fills}; on a book received after the signal: "
        f"{report.fills_on_book_after_signal}; on the same or an older book: "
        f"{report.fills_on_book_not_after_signal} "
        "(sampled data cannot show the market moving inside the latency)",
        f"  maker fills             {report.maker_fills}",
        "",
        "Data",
        f"  events                  init {report.initialization_events}, accepted "
        f"{report.events_accepted}, rejected {report.events_rejected}, replayed "
        f"{report.events_replayed}",
        f"  fingerprint             {report.dataset_fingerprint or 'n/a'}",
        f"  issues                  {(report.dataset_issues or {}).get('counts', {})}",
        f"  incomplete components   {report.incomplete_components}",
    ]
    lines += [f"  warning: {warning}" for warning in report.warnings]
    return "\n".join(lines)
