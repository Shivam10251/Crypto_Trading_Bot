"""Recorded datasets for replay tests, written to the real test database.

Clearly synthetic: every market lives on the venue ``replaytest``, which no
live service ever registers, and every row is deleted again after the test.
A backtest commits through its own engine, so seeding has to commit too -
the rolled-back ``db`` fixture would be invisible to it.

``DatasetBuilder`` produces what capture would have recorded: a quote and a
depth snapshot per market per step, and funding observations on a slower
cadence, all timestamped by *local receipt* - the clock replay orders by.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tests.integration.conftest import TEST_URL
from trading_bot.core.config import Settings
from trading_bot.db.models import (
    BacktestRun,
    FundingObservation,
    Market,
    MarketData,
    OrderBookSnapshot,
    SystemEvent,
)
from trading_bot.db.models.enums import MarketType
from trading_bot.exchange.models import MarketRef

VENUE = "replaytest"
START = datetime(2026, 8, 3, 7, 59, 0, tzinfo=UTC)
SPOT = MarketRef(VENUE, "BTCUSDT", MarketType.SPOT)
PERP = MarketRef(VENUE, "BTCUSDT", MarketType.PERPETUAL)
PAIR = (SPOT, PERP)
EIGHT_HOURS = timedelta(hours=8)


def backtest_settings(**sections: dict[str, Any]) -> Settings:
    """Replay settings with fees and buffers zeroed unless a test says otherwise.

    Zero costs isolate the pipeline's mechanics from the fee schedule, so a
    test about latency or depth is not also a test about the 30 bps floor.
    """
    base: dict[str, dict[str, Any]] = {
        "database": {"url_override": TEST_URL},
        "exchange": {"venue": VENUE},
        "costs": {
            "spot_taker_fee_bps": 0.0,
            "perp_taker_fee_bps": 0.0,
            "spot_maker_fee_bps": 0.0,
            "perp_maker_fee_bps": 0.0,
            "safety_buffer_bps": 0.0,
            "funding_horizon_minutes": 1.0,
        },
        "strategy": {
            "evaluate_interval_ms": 1000,
            "spot_perp_basis": {
                "min_net_edge_bps": 1.0,
                # Below the 1,000 USD order limit even after a premium leg's
                # price rounds the notional up.
                "max_notional_usd": 900.0,
                "signal_ttl_ms": 500,
            },
        },
        "execution": {"latency_ms": 100, "latency_jitter_ms": 0, "timeout_ms": 5000},
        "portfolio": {
            "snapshot_interval_ms": 60_000,
            "min_return_observations": 2,
            "exits": {"evaluate_interval_ms": 1000, "target_basis_bps": 2.0},
        },
        "risk": {"daily_loss_policy": "deferred", "consecutive_loss_policy": "deferred"},
        "backtest": {"heartbeat_seconds": 0.5, "orphan_after_seconds": 5.0},
    }
    for name, values in sections.items():
        base[name] = _merge(base.get(name, {}), values)
    return Settings(**base)  # type: ignore[arg-type]


def _merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge(merged[key], value)
        else:
            merged[key] = value
    return merged


@dataclass
class DatasetBuilder:
    quotes: list[dict[str, Any]] = field(default_factory=list)
    books: list[dict[str, Any]] = field(default_factory=list)
    funding: list[dict[str, Any]] = field(default_factory=list)
    _sequence: dict[MarketRef, int] = field(default_factory=dict)

    def quote(
        self, ref: MarketRef, at: datetime, bid: str, ask: str, size: str = "5", **extra: Any
    ) -> None:
        self.quotes.append(
            {
                "ref": ref,
                "bid": Decimal(bid),
                "ask": Decimal(ask),
                "bid_size": Decimal(size),
                "ask_size": Decimal(size),
                "local_timestamp": at,
                "sequence": self._next(ref),
                **extra,
            }
        )

    def book(
        self,
        ref: MarketRef,
        at: datetime,
        bid: str,
        ask: str,
        *,
        levels: int = 5,
        size: str = "1",
        tick: str = "1",
        sequence: int | None = None,
        **extra: Any,
    ) -> None:
        step = Decimal(tick)
        best_bid, best_ask = Decimal(bid), Decimal(ask)
        self.books.append(
            {
                "ref": ref,
                "bids": [[str(best_bid - step * i), size] for i in range(levels)],
                "asks": [[str(best_ask + step * i), size] for i in range(levels)],
                "depth_levels": levels,
                "local_timestamp": at,
                "sequence": sequence if sequence is not None else self._next(ref),
                **extra,
            }
        )

    def observe_funding(
        self,
        ref: MarketRef,
        at: datetime,
        *,
        rate: str = "0.0001",
        mark: str = "100000",
        next_funding_time: datetime | None = None,
        interval_hours: int | None = 8,
    ) -> None:
        settles = next_funding_time or next_settlement(at)
        self.funding.append(
            {
                "ref": ref,
                "mark_price": Decimal(mark),
                "index_price": Decimal(mark),
                "funding_rate": Decimal(rate),
                "next_funding_time": settles,
                "funding_interval_hours": interval_hours,
                "local_timestamp": at,
            }
        )

    def market_pair(
        self,
        at: datetime,
        *,
        spot_mid: Decimal,
        basis_bps: Decimal,
        half_spread: Decimal = Decimal("0.5"),
        size: str = "1",
        levels: int = 5,
    ) -> None:
        """One step of both legs: a quote and a book each, at one receipt time."""
        perp_mid = spot_mid * (1 + basis_bps / Decimal(10_000))
        for ref, mid in ((SPOT, spot_mid), (PERP, perp_mid)):
            bid = str((mid - half_spread).quantize(Decimal("0.1")))
            ask = str((mid + half_spread).quantize(Decimal("0.1")))
            self.quote(ref, at, bid, ask, size=size)
            self.book(ref, at, bid, ask, levels=levels, size=size)

    def _next(self, ref: MarketRef) -> int:
        self._sequence[ref] = self._sequence.get(ref, 1000) + 1
        return self._sequence[ref]


def next_settlement(at: datetime) -> datetime:
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    elapsed = (at - epoch) // EIGHT_HOURS + 1
    return epoch + elapsed * EIGHT_HOURS


def run_async[T](factory: Callable[[AsyncSession], Awaitable[T]]) -> T:
    """Run ``factory`` against the test database on a fresh, ordinary loop."""

    async def main() -> T:
        engine = create_async_engine(TEST_URL)
        try:
            sessions = async_sessionmaker(engine, expire_on_commit=False)
            async with sessions() as session:
                result = await factory(session)
                await session.commit()
                return result
        finally:
            await engine.dispose()

    return asyncio.run(main())


def seed(dataset: DatasetBuilder, refs: tuple[MarketRef, ...] = PAIR) -> dict[MarketRef, int]:
    async def write(session: AsyncSession) -> dict[MarketRef, int]:
        ids: dict[MarketRef, int] = {}
        for ref in refs:
            market = Market(
                venue=ref.venue,
                symbol=ref.symbol,
                market_type=ref.market_type,
                base_asset="BTC",
                quote_asset="USDT",
                tick_size=Decimal("0.1"),
                step_size=Decimal("0.001"),
                min_notional=Decimal("5"),
                min_qty=Decimal("0.001"),
                is_active=True,
            )
            session.add(market)
            await session.flush()
            ids[ref] = market.id
        for row in dataset.quotes:
            values = {key: value for key, value in row.items() if key != "ref"}
            mid = (values["bid"] + values["ask"]) / 2
            session.add(
                MarketData(
                    market_id=ids[row["ref"]],
                    mid_price=mid,
                    spread=values["ask"] - values["bid"],
                    spread_bps=(values["ask"] - values["bid"]) / mid * 10_000,
                    **values,
                )
            )
        for row in dataset.books:
            values = {key: value for key, value in row.items() if key != "ref"}
            session.add(OrderBookSnapshot(market_id=ids[row["ref"]], **values))
        for row in dataset.funding:
            values = {key: value for key, value in row.items() if key != "ref"}
            session.add(FundingObservation(market_id=ids[row["ref"]], **values))
        return ids

    return run_async(write)


def cleanup() -> None:
    """Delete every replay-test run (cascading to artifacts) and market."""

    async def wipe(session: AsyncSession) -> None:
        await session.execute(delete(BacktestRun))
        await session.execute(delete(SystemEvent).where(SystemEvent.component == "replay_capture"))
        markets = (await session.execute(select(Market.id).where(Market.venue == VENUE))).scalars()
        ids = list(markets)
        if ids:
            await session.execute(delete(Market).where(Market.id.in_(ids)))

    run_async(wipe)


def factory_for(sessions: async_sessionmaker[AsyncSession]) -> Callable[[], Any]:
    """A commit-on-exit session factory, like ``session_scope``."""
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def scope() -> Any:
        async with sessions() as session:
            yield session
            await session.commit()

    return scope


def with_factory[T](work: Callable[[Callable[[], Any]], Awaitable[T]]) -> T:
    async def main() -> T:
        engine = create_async_engine(TEST_URL)
        try:
            return await work(factory_for(async_sessionmaker(engine, expire_on_commit=False)))
        finally:
            await engine.dispose()

    return asyncio.run(main())


def normalized_results(run_id: int) -> dict[str, list[tuple[Any, ...]]]:
    """Everything a run produced that describes the market, minus row ids and wall clock."""
    from trading_bot.db.models import (
        BacktestFundingPayment,
        Fill,
        Opportunity,
        Order,
        PnlSnapshot,
        PortfolioSnapshot,
        Position,
        RiskEvent,
        Signal,
    )

    async def read(session: AsyncSession) -> dict[str, list[tuple[Any, ...]]]:
        async def rows(statement: Any) -> list[tuple[Any, ...]]:
            return sorted(
                (tuple(row) for row in (await session.execute(statement)).all()), key=repr
            )

        return {
            "funding_payments": await rows(
                select(
                    BacktestFundingPayment.settled_at,
                    BacktestFundingPayment.quantity,
                    BacktestFundingPayment.rate,
                    BacktestFundingPayment.mark_price,
                    BacktestFundingPayment.amount_usd,
                ).where(BacktestFundingPayment.backtest_run_id == run_id)
            ),
            "opportunities": await rows(
                select(
                    Opportunity.uid,
                    Opportunity.status,
                    Opportunity.rejection_reason,
                    Opportunity.net_edge_bps,
                    Opportunity.detected_at,
                    Opportunity.samples,
                ).where(Opportunity.backtest_run_id == run_id)
            ),
            "signals": await rows(
                select(Signal.generated_at, Signal.side, Signal.quantity, Signal.expires_at).where(
                    Signal.backtest_run_id == run_id
                )
            ),
            "risk_events": await rows(
                select(
                    RiskEvent.intent_id,
                    RiskEvent.event_type,
                    RiskEvent.decision,
                    RiskEvent.occurred_at,
                    RiskEvent.reason,
                ).where(RiskEvent.backtest_run_id == run_id)
            ),
            "orders": await rows(
                select(
                    Order.client_order_id,
                    Order.exchange_order_id,
                    Order.intent,
                    Order.status,
                    Order.filled_quantity,
                    Order.average_fill_price,
                    Order.submitted_at,
                    Order.acknowledged_at,
                    Order.book_sequence,
                ).where(Order.backtest_run_id == run_id)
            ),
            "fills": await rows(
                select(
                    Fill.price, Fill.quantity, Fill.fee_usd, Fill.filled_at, Fill.book_sequence
                ).where(Fill.backtest_run_id == run_id)
            ),
            "positions": await rows(
                select(
                    Position.attempt_id,
                    Position.status,
                    Position.side,
                    Position.realized_pnl_usd,
                    Position.funding_pnl_usd,
                    Position.opened_at,
                    Position.closed_at,
                    Position.exit_reason,
                    Position.close_claim_id,
                ).where(Position.backtest_run_id == run_id)
            ),
            "portfolio_snapshots": await rows(
                select(
                    PortfolioSnapshot.captured_at,
                    PortfolioSnapshot.equity_usd,
                    PortfolioSnapshot.cash_usd,
                    PortfolioSnapshot.valuation_status,
                ).where(PortfolioSnapshot.backtest_run_id == run_id)
            ),
            "pnl_snapshots": await rows(
                select(
                    PnlSnapshot.captured_at,
                    PnlSnapshot.window,
                    PnlSnapshot.scope_key,
                    PnlSnapshot.realized_pnl_usd,
                    PnlSnapshot.trade_count,
                    PnlSnapshot.max_drawdown_usd,
                ).where(
                    PnlSnapshot.backtest_run_id == run_id, ~PnlSnapshot.scope_key.like("position:%")
                )
            ),
        }

    return run_async(read)


def basis_dataset(basis_bps: int, *, seconds: int = 1, start: datetime = START) -> DatasetBuilder:
    """A steady basis, one pair per second, funding at the start."""
    dataset = DatasetBuilder()
    for second in range(seconds):
        at = start + timedelta(seconds=second)
        dataset.market_pair(at, spot_mid=Decimal(100_000), basis_bps=Decimal(basis_bps))
        if second % 30 == 0:
            dataset.observe_funding(PERP, at, mark="100100")
    return dataset
