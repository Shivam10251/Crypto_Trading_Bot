"""The same market, seen live and replayed, produces the same strategy decisions.

Paper side: the real ``MarketDataEngine`` - real parser, connections, book
synchronisation - fed Binance-shaped messages over fake sockets, exactly as its
own tests drive it, with the strategy runner evaluating its snapshots.

Replay side: only what Phase 11's capture would have stored from that same
engine (``capture.book_row`` from its execution view, its quotes, the funding
observation), turned back into events by the PostgreSQL source's own row
converters, and evaluated by an identical runner against ``ReplayMarketData``.

If replay presented the strategy with anything other than what the live
engine did - different ages, statuses, depth, liquidity or timing - these
decisions would diverge.

One divergence is real and pinned down here rather than hidden: the live
engine marks every market on a connection ``STALE`` once the *connection* has
been silent for ``stale_after_ms``. Sampled rows cannot show a connection's
heartbeat - an unchanged book writes no row - so replay judges each input by
its own age instead. Both refuse the pair; they name it differently.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from decimal import Decimal
from itertools import count
from types import SimpleNamespace
from typing import Any

from tests.unit.test_market_data_engine import (
    PERP,
    SPOT,
    START,
    Clock,
    book_ticker,
    eventually,
    fast_config,
    running,
)
from trading_bot.backtest.clock import ReplayClock
from trading_bot.backtest.events import ReplayEvent
from trading_bot.backtest.market_state import CarryLimits, ReplayMarketData
from trading_bot.backtest.postgres_source import _book_event, _funding_event, _quote_event
from trading_bot.backtest.source import DatasetIssues
from trading_bot.core.config import CostsConfig, MonitoringConfig, SpotPerpBasisConfig
from trading_bot.exchange.models import BookLevel, FundingInfo, MarketDataSubscription, OrderBook
from trading_bot.marketdata.capture import book_row, funding_row
from trading_bot.monitoring.monitor import MarketMonitor
from trading_bot.strategy.base import StrategyContext
from trading_bot.strategy.basis import SpotPerpBasisStrategy
from trading_bot.strategy.costs import TransactionCostModel
from trading_bot.strategy.models import RejectionReason
from trading_bot.strategy.runner import StrategyEvaluation, StrategyRunner

DEPTH = 5
CONFIG = fast_config(depth_levels=DEPTH, snapshot_depth=1000)
FUNDING = FundingInfo(
    ref=PERP,
    mark_price=Decimal(105),
    index_price=Decimal(100),
    last_funding_rate=Decimal("0.0001"),
    next_funding_time=START + timedelta(hours=4),
    local_timestamp=START,
    funding_interval_hours=8,
)


def ladder(ref: Any, center: int, sequence: int) -> OrderBook:
    return OrderBook(
        ref=ref,
        bids=tuple(BookLevel(Decimal(center - i), Decimal(2)) for i in range(1, 11)),
        asks=tuple(BookLevel(Decimal(center + i), Decimal(2)) for i in range(1, 11)),
        local_timestamp=START,
        sequence=sequence,
    )


def depth(ref: Any, update_id: int, bids: tuple[tuple[str, str], ...], at: Any) -> dict[str, Any]:
    """A depth diff stamped with the venue clock at ``at``, as Binance sends it."""
    data = {
        "e": "depthUpdate",
        "E": int(at.timestamp() * 1000),
        "s": ref.symbol,
        "U": update_id,
        "u": update_id,
        "b": [list(level) for level in bids],
        "a": [],
    }
    return {"stream": f"{ref.symbol.lower()}@depth@100ms", "data": data}


def runner(clock: Any) -> StrategyRunner:
    costs = TransactionCostModel(
        CostsConfig(
            spot_taker_fee_bps=0.0,
            perp_taker_fee_bps=0.0,
            safety_buffer_bps=0.0,
        )
    )
    strategy = SpotPerpBasisStrategy(
        SpotPerpBasisConfig(max_notional_usd=500.0, max_quote_age_ms=1_500, max_book_age_ms=1_500)
    )
    return StrategyRunner([strategy], StrategyContext(cost_model=costs), clock=clock)


def decisions(evaluations: list[StrategyEvaluation]) -> list[tuple[object, ...]]:
    rows: list[tuple[object, ...]] = []
    for evaluation in evaluations:
        stats = evaluation.stats
        rows.append(
            (
                "stats",
                stats.pairs_usable if stats else None,
                dict(stats.unusable) if stats else None,
            )
        )
        for item in evaluation.opportunities:
            opportunity = item.opportunity
            rows.append(
                (
                    evaluation.evaluated_at,
                    item.rejection,
                    item.net_edge_bps,
                    opportunity.gross_edge_bps,
                    opportunity.quantity,
                    opportunity.buy.executable_price,
                    opportunity.sell.executable_price,
                    opportunity.buy.unwind_price,
                    opportunity.liquidity_usd,
                    item.signal is not None,
                )
            )
    return rows


async def live_run(clock: Clock) -> tuple[list[Any], list[ReplayEvent]]:
    """Drive the engine; return its decisions and what capture would have stored."""
    subscription = MarketDataSubscription(refs=(SPOT, PERP), include_depth=True, depth_levels=DEPTH)
    ids = count(1)
    recorded: list[ReplayEvent] = []
    seen: dict[Any, tuple[object, object]] = {}
    live_runner = runner(clock)
    live_runner.set_funding({PERP: FUNDING})
    results: list[Any] = []
    books = {SPOT: [ladder(SPOT, 100, 100)], PERP: [ladder(PERP, 120, 500)]}

    def capture(engine: Any) -> None:
        batch: list[ReplayEvent] = []
        for ref in (SPOT, PERP):
            book = engine.execution_snapshot(ref).book
            quote = engine.snapshot(ref).quote
            last_book, last_quote = seen.get(ref, (None, None))
            if book is not None and book is not last_book:
                row = book_row(0, book, 1000)
                batch.append(_book_event(SimpleNamespace(id=next(ids), **row), ref))
            if quote is not None and quote is not last_quote:
                batch.append(
                    _quote_event(
                        SimpleNamespace(
                            id=next(ids),
                            bid=quote.bid,
                            ask=quote.ask,
                            bid_size=quote.bid_size,
                            ask_size=quote.ask_size,
                            local_timestamp=quote.local_timestamp,
                            exchange_timestamp=quote.exchange_timestamp,
                            sequence=quote.sequence,
                            volume_24h=None,
                        ),
                        ref,
                    )
                )
            seen[ref] = (book, quote)
        recorded.extend(sorted(batch, key=lambda event: event.order_key))

    async with running(subscription, snapshots=books, config=CONFIG, clock=clock) as h:
        monitor = MarketMonitor(h.engine, MonitoringConfig(), clock=clock)
        await h.venue.wait_connected("spot", "perp-public")
        steps = [
            # (spot quote, perp quote, perp depth change, seconds until the evaluation)
            (("99", "101"), ("119", "121"), None, 0.2),  # a wide, tradeable basis
            (("99", "101"), ("103", "105"), (("118", "1"),), 0.5),  # basis mostly gone
            (("99", "101"), ("119", "121"), None, 1.8),  # inputs older than 1.5 s
            (("98", "100"), ("117", "119"), (("117", "5"),), 0.1),
        ]
        update = {SPOT: 101, PERP: 501}
        for index, (spot_quote, perp_quote, perp_bids, wait) in enumerate(steps):
            for ref, route, side in ((SPOT, "spot", spot_quote), (PERP, "perp-public", perp_quote)):
                bids = perp_bids if ref is PERP and perp_bids else ((str(95 + index), "2"),)
                h.venue.send(route, depth(ref, update[ref], bids, clock.now))
                update[ref] += 1
                h.venue.send(route, book_ticker(ref, update[ref] + 10_000, *side))
            for route in ("spot", "perp-public"):
                await h.venue.drained(route)
            await eventually(
                lambda: all(
                    (book := h.engine.execution_snapshot(ref).book) is not None
                    and book.sequence == update[ref] - 1
                    for ref in (SPOT, PERP)
                )
            )
            capture(h.engine)
            clock.advance(int(wait * 1000))
            monitor.sample()
            results.append(
                (
                    clock.now,
                    decisions(live_runner.evaluate(h.engine.snapshots(), monitor.metrics())),
                )
            )
            clock.advance(400)
        # The documented divergence: a connection silent past stale_after_ms.
        clock.advance(CONFIG.stale_after_ms + 500)
        results.append(
            (clock.now, decisions(live_runner.evaluate(h.engine.snapshots(), monitor.metrics())))
        )
    return results, recorded


def test_replay_reproduces_the_live_engines_decisions() -> None:
    clock = Clock(START + timedelta(seconds=1))
    live, recorded = asyncio.run(live_run(clock))
    assert len(live) == 5
    assert any(row[-1] for row in live[0][1] if row[0] != "stats"), "the scenario needs a signal"
    replay_clock = ReplayClock(START)
    market = ReplayMarketData(
        (SPOT, PERP),
        replay_clock,
        CarryLimits(
            depth_levels=DEPTH,
            market_silence_ms=CONFIG.market_silence_ms,
            max_book_carry_ms=60_000,
            max_funding_carry_ms=600_000,
            liquidity_band_bps=Decimal(str(CONFIG.liquidity_band_bps)),
            reference_notional=Decimal(str(CONFIG.reference_order_notional)),
        ),
        DatasetIssues(),
    )
    funding = _funding_event(SimpleNamespace(id=0, **funding_row(0, FUNDING)), PERP)
    market.extend([funding, *recorded])
    market.mark_exhausted()
    replay_runner = runner(replay_clock)
    monitor = MarketMonitor(market, MonitoringConfig(), clock=replay_clock)
    replayed: list[Any] = []
    for moment, _ in live:
        replay_clock.advance_to(moment)
        replay_runner.set_funding(market.rates)
        monitor.sample()
        replayed.append(decisions(replay_runner.evaluate(market.snapshots(), monitor.metrics())))
    silent = live.pop()[1]
    replay_silent = replayed.pop()
    assert replayed == [rows for _, rows in live]
    # Silence: live calls the market not live; replay calls its data stale.
    assert silent == [("stats", 0, {RejectionReason.NOT_LIVE: 1})]
    assert replay_silent == [("stats", 0, {RejectionReason.STALE_DATA: 1})]
