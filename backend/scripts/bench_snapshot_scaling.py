"""How a portfolio snapshot's cost grows with a run's history.

Phase 10 documented that ``PortfolioService.snapshot`` reads every closed
trade and the whole equity curve on every call. That is harmless for a paper
service and quadratic for a backtest, which writes a snapshot per interval
for as long as it replays. This measures it rather than assuming it: a run
scope is populated directly with N completed paired trades and S equity
snapshots, and one more snapshot is timed.

    TB_DATABASE__PASSWORD=... uv run python scripts/bench_snapshot_scaling.py

Writes only to ``trading_bot_test`` under a synthetic venue, and deletes it.
"""

from __future__ import annotations

import asyncio
import os
import statistics
import sys
import time
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import delete, insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from trading_bot.core.config import PortfolioConfig
from trading_bot.db.models import BacktestRun, Fill, Market, Order, PortfolioSnapshot, Position
from trading_bot.db.models.enums import (
    BacktestRunStatus,
    ExecutionMode,
    MarketType,
    OrderStatus,
    OrderType,
    PositionStatus,
    Side,
    ValuationStatus,
)
from trading_bot.portfolio.service import PortfolioService
from trading_bot.portfolio.snapshots import SnapshotWriter
from trading_bot.portfolio.store import PortfolioStore
from trading_bot.portfolio.valuation import MarkReader

URL = "postgresql+asyncpg://{user}:{password}@{host}:{port}/trading_bot_test".format(
    user=os.environ.get("TB_DATABASE__USER", "trading_bot"),
    password=os.environ.get("TB_DATABASE__PASSWORD", "change_me_locally"),
    host=os.environ.get("TB_DATABASE__HOST", "127.0.0.1"),
    port=os.environ.get("TB_DATABASE__PORT", "5432"),
)
VENUE = "benchsnapshot"
START = datetime(2026, 1, 1, tzinfo=UTC)


class _NoFeed:
    def snapshot(self, ref: object) -> object:
        raise LookupError(ref)


async def populate(sessions: async_sessionmaker[AsyncSession], trades: int, snapshots: int) -> int:
    async with sessions() as session:
        run = BacktestRun(
            run_uid=uuid.uuid4(),
            status=BacktestRunStatus.RUNNING,
            dataset_source="bench",
            requested_start=START,
            requested_end=START + timedelta(days=30),
            markets=[],
            config_snapshot={},
            config_hash="0" * 64,
            started_at=START,
        )
        session.add(run)
        spot = Market(
            venue=VENUE,
            symbol="BTCUSDT",
            market_type=MarketType.SPOT,
            base_asset="BTC",
            quote_asset="USDT",
        )
        perp = Market(
            venue=VENUE,
            symbol="BTCUSDT",
            market_type=MarketType.PERPETUAL,
            base_asset="BTC",
            quote_asset="USDT",
        )
        session.add_all([spot, perp])
        await session.flush()
        scope = {"mode": ExecutionMode.BACKTEST, "backtest_run_id": run.id}
        for chunk in range(0, trades, 500):
            positions, orders = [], []
            for index in range(chunk, min(trades, chunk + 500)):
                opened = START + timedelta(seconds=30 * index)
                closed = opened + timedelta(seconds=10)
                attempt = f"a{index}"
                for market, side, entry, exit_ in (
                    (spot, Side.BUY, "100000", "100010"),
                    (perp, Side.SELL, "100020", "100015"),
                ):
                    positions.append(
                        {
                            **scope,
                            "market_id": market.id,
                            "attempt_id": attempt,
                            "strategy": "s",
                            "side": side,
                            "status": PositionStatus.CLOSED,
                            "quantity": Decimal("0.01"),
                            "closed_quantity": Decimal("0.01"),
                            "entry_price": Decimal(entry),
                            "exit_price": Decimal(exit_),
                            "entry_notional_usd": Decimal(entry) / 100,
                            "exit_notional_usd": Decimal(exit_) / 100,
                            "realized_pnl_usd": Decimal("0.1"),
                            "opened_at": opened,
                            "closed_at": closed,
                        }
                    )
            ids = (
                await session.execute(
                    insert(Position).returning(
                        Position.id,
                        Position.attempt_id,
                        Position.market_id,
                        Position.side,
                        Position.entry_price,
                        Position.exit_price,
                        Position.opened_at,
                        Position.closed_at,
                    ),
                    positions,
                )
            ).all()
            for pid, attempt, market_id, side, entry, exit_, opened, closed in ids:
                for intent, order_side, price, at in (
                    ("OPEN", side, entry, opened),
                    ("CLOSE", Side.SELL if side is Side.BUY else Side.BUY, exit_, closed),
                ):
                    orders.append(
                        (
                            pid,
                            {
                                **scope,
                                "market_id": market_id,
                                "client_order_id": f"{attempt}-{market_id}-{intent}",
                                "execution_intent_id": f"{attempt}-{intent}",
                                "signal_leg": market_id,
                                "attempt_id": attempt,
                                "intent": intent,
                                "side": order_side,
                                "order_type": OrderType.MARKET,
                                "quantity": Decimal("0.01"),
                                "filled_quantity": Decimal("0.01"),
                                "status": OrderStatus.FILLED,
                                "expected_price": price,
                            },
                            price,
                            at,
                        )
                    )
            order_ids = (
                (
                    await session.execute(
                        insert(Order).returning(Order.id), [row for _, row, _, _ in orders]
                    )
                )
                .scalars()
                .all()
            )
            await session.execute(
                insert(Fill),
                [
                    {
                        **scope,
                        "order_id": oid,
                        "position_id": pid,
                        "price": price,
                        "quantity": Decimal("0.01"),
                        "fee_usd": Decimal("0.01"),
                        "fee_asset": "USDT",
                        "filled_at": at,
                        "fill_index": 0,
                    }
                    for oid, (pid, _, price, at) in zip(order_ids, orders, strict=True)
                ],
            )
        rows = [
            {
                **scope,
                "captured_at": START + timedelta(minutes=i),
                "cash_usd": Decimal(100000),
                "position_value_usd": Decimal(0),
                "equity_usd": Decimal(100000) + Decimal(i % 7),
                "valuation_status": ValuationStatus.COMPLETE,
            }
            for i in range(snapshots)
        ]
        for chunk in range(0, len(rows), 5000):
            await session.execute(insert(PortfolioSnapshot), rows[chunk : chunk + 5000])
        await session.commit()
        return run.id


async def measure(trades: int, snapshots: int, *, incremental: bool = False) -> float:
    engine = create_async_engine(URL)
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    def factory():  # a commit-on-exit session, like session_scope
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def scope():
            async with sessions() as session:
                yield session
                await session.commit()

        return scope()

    try:
        run_id = await populate(sessions, trades, snapshots)
        store = PortfolioStore(factory, mode=ExecutionMode.BACKTEST, backtest_run_id=run_id)
        config = PortfolioConfig(snapshot_interval_ms=60_000, min_return_observations=30)
        writer = SnapshotWriter(
            store,
            factory,
            initial_cash_usd=Decimal(100000),
            fees_paid_in_cash=True,
            interval=timedelta(minutes=1),
            min_return_observations=30,
            risk_free_rate_annual_pct=0.0,
        )
        moment = START + timedelta(minutes=snapshots + 1)
        clock = [moment]
        service = PortfolioService(
            store=store,
            writer=writer,
            marks=MarkReader(_NoFeed(), max_book_age_ms=1000),
            closer=None,
            pnl_source=None,
            config=config,
            venue=VENUE,
            clock=lambda: clock[0],
            incremental=incremental,
        )
        if incremental:
            await service.snapshot()  # folds the whole history once, as a run would have
        timings = []
        for step in range(3):
            clock[0] = moment + timedelta(minutes=step + 1)
            began = time.perf_counter()
            await service.snapshot()
            timings.append(time.perf_counter() - began)
        return statistics.median(timings)
    finally:
        async with sessions() as session:
            await session.execute(delete(BacktestRun).where(BacktestRun.dataset_source == "bench"))
            await session.execute(delete(Market).where(Market.venue == VENUE))
            await session.commit()
        await engine.dispose()


async def main(grid: list[tuple[int, int]]) -> None:
    print(f"{'closed trades':>14} {'snapshots':>10} {'history (ms)':>13} {'folded (ms)':>12}")
    for trades, snapshots in grid:
        history = await measure(trades, snapshots)
        folded = await measure(trades, snapshots, incremental=True)
        print(f"{trades:>14} {snapshots:>10} {history * 1000:>13.1f} {folded * 1000:>12.1f}")


if __name__ == "__main__":
    points = [(0, 0), (250, 1440), (1000, 1440), (4000, 1440), (1000, 10080), (1000, 43200)]
    asyncio.run(
        main(
            points
            if len(sys.argv) == 1
            else [tuple(map(int, arg.split(","))) for arg in sys.argv[1:]]
        )
    )
