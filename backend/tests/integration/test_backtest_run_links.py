"""Provenance links cannot cross a run, whoever writes them.

Every link is checked by PostgreSQL itself, with raw SQL rather than the
stores, because the point is that a writer that forgot to scope - or never
knew to - is refused.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.integration.factories import make_market
from tests.integration.test_backtest_persistence import make_run, violates

pytestmark = pytest.mark.requires_postgres

Scope = tuple[str, int | None]
PAPER: Scope = ("PAPER", None)


async def scalar(db: AsyncSession, sql: str, **params: Any) -> Any:
    return (await db.execute(text(sql), params)).scalar_one()


class Rows:
    """Minimal raw rows, each in a given scope."""

    def __init__(self, db: AsyncSession, market_id: int) -> None:
        self.db, self.market = db, market_id
        self._n = 0

    def _next(self) -> int:
        self._n += 1
        return self._n

    async def opportunity(self, scope: Scope) -> int:
        return await scalar(
            self.db,
            "INSERT INTO opportunities (detected_at, strategy, mode, backtest_run_id, market_id, "
            "direction, entry_price, quantity, notional_usd, gross_edge_usd, gross_edge_bps, "
            "status, uid, net_edge_bps) VALUES (now(), 's', :mode, :run, :market, 'BUY', 1, 1, "
            "1, 0, 0, 'DETECTED', gen_random_uuid(), 0) RETURNING id",
            mode=scope[0] if scope[0] != "PAPER" else "THEORETICAL",
            run=scope[1],
            market=self.market,
        )

    async def signal(self, scope: Scope, opportunity: int) -> int:
        return await scalar(
            self.db,
            "INSERT INTO signals (opportunity_id, backtest_run_id, generated_at, strategy, "
            "market_id, side, quantity, target_entry_price, expected_net_edge_bps, status) "
            "VALUES (:opp, :run, now(), 's', :market, :side, 1, 1, 0, 'GENERATED') RETURNING id",
            side="BUY" if self._next() % 2 else "SELL",
            opp=opportunity,
            run=scope[1],
            market=self.market,
        )

    async def risk_event(self, scope: Scope, signal: int | None = None) -> int:
        return await scalar(
            self.db,
            "INSERT INTO risk_events (occurred_at, mode, backtest_run_id, event_type, decision, "
            "intent_id, reason, signal_id) VALUES (now(), :mode, :run, 'PRE_TRADE_CHECK', "
            "'APPROVED', :intent, 'ok', :signal) RETURNING id",
            mode=scope[0],
            run=scope[1],
            intent=f"i{self._next()}",
            signal=signal,
        )

    async def order(
        self, scope: Scope, *, signal: int | None = None, risk_event: int | None = None
    ) -> int:
        return await scalar(
            self.db,
            "INSERT INTO orders (market_id, mode, backtest_run_id, client_order_id, side, "
            "order_type, quantity, filled_quantity, status, signal_id, risk_event_id) VALUES "
            "(:market, :mode, :run, :client, 'BUY', 'MARKET', 1, 0, 'PENDING', :signal, :risk) "
            "RETURNING id",
            market=self.market,
            mode=scope[0],
            run=scope[1],
            client=f"c{self._next()}",
            signal=signal,
            risk=risk_event,
        )

    async def position(self, scope: Scope, opportunity: int | None = None) -> int:
        return await scalar(
            self.db,
            "INSERT INTO positions (market_id, mode, backtest_run_id, attempt_id, strategy, side, "
            "status, quantity, entry_price, entry_notional_usd, realized_pnl_usd, "
            "unrealized_pnl_usd, fees_usd, slippage_usd, opened_at, opportunity_id) VALUES "
            "(:market, :mode, :run, :attempt, 's', 'BUY', 'OPEN', 1, 1, 1, 0, 0, 0, 0, now(), "
            ":opp) RETURNING id",
            market=self.market,
            mode=scope[0],
            run=scope[1],
            attempt=f"a{self._next()}",
            opp=opportunity,
        )

    async def fill(self, scope: Scope, order: int, position: int | None = None) -> int:
        return await scalar(
            self.db,
            "INSERT INTO fills (order_id, mode, backtest_run_id, position_id, price, quantity, "
            "fee_usd, filled_at, fill_index) VALUES (:order, :mode, :run, :position, 1, 1, 0, "
            "now(), :index) RETURNING id",
            order=order,
            mode=scope[0],
            run=scope[1],
            position=position,
            index=self._next(),
        )

    async def pnl(self, scope: Scope, position: int) -> int:
        return await scalar(
            self.db,
            "INSERT INTO pnl_snapshots (captured_at, mode, backtest_run_id, position_id, "
            'scope_key, "window", realized_pnl_usd, fees_usd, slippage_usd, trade_count, '
            "winning_trades, losing_trades) VALUES (now(), :mode, :run, :position, :key, 'all', "
            "0, 0, 0, 0, 0, 0) RETURNING id",
            mode=scope[0],
            run=scope[1],
            position=position,
            key=f"position:{position}",
        )


@pytest.fixture
async def rows(db: AsyncSession) -> Rows:
    market = make_market(venue="links")
    db.add(market)
    await db.flush()
    return Rows(db, market.id)


async def two_runs(db: AsyncSession) -> tuple[Scope, Scope]:
    first, second = await make_run(db), await make_run(db)
    return ("BACKTEST", first.id), ("BACKTEST", second.id)


Link = Callable[[Rows, Scope, Scope], Awaitable[Any]]


async def signal_to_opportunity(rows: Rows, parent: Scope, child: Scope) -> Any:
    return await rows.signal(child, await rows.opportunity(parent))


async def risk_event_to_signal(rows: Rows, parent: Scope, child: Scope) -> Any:
    signal = await rows.signal(parent, await rows.opportunity(parent))
    return await rows.risk_event(child, signal)


async def order_to_signal(rows: Rows, parent: Scope, child: Scope) -> Any:
    signal = await rows.signal(parent, await rows.opportunity(parent))
    return await rows.order(child, signal=signal)


async def order_to_risk_event(rows: Rows, parent: Scope, child: Scope) -> Any:
    return await rows.order(child, risk_event=await rows.risk_event(parent))


async def fill_to_order(rows: Rows, parent: Scope, child: Scope) -> Any:
    return await rows.fill(child, await rows.order(parent))


async def fill_to_position(rows: Rows, parent: Scope, child: Scope) -> Any:
    return await rows.fill(child, await rows.order(child), await rows.position(parent))


async def position_to_opportunity(rows: Rows, parent: Scope, child: Scope) -> Any:
    return await rows.position(child, await rows.opportunity(parent))


async def pnl_to_position(rows: Rows, parent: Scope, child: Scope) -> Any:
    return await rows.pnl(child, await rows.position(parent))


LINKS: list[tuple[Link, str]] = [
    (signal_to_opportunity, "fk_signals_opportunity_id_run_key_opportunities"),
    (risk_event_to_signal, "fk_risk_events_signal_id_run_key_signals"),
    (order_to_signal, "fk_orders_signal_id_run_key_signals"),
    (order_to_risk_event, "fk_orders_risk_event_id_run_key_risk_events"),
    (fill_to_order, "fk_fills_order_id_run_key_orders"),
    (fill_to_position, "fk_fills_position_id_run_key_positions"),
    (position_to_opportunity, "fk_positions_opportunity_id_run_key_opportunities"),
    (pnl_to_position, "fk_pnl_snapshots_position_id_run_key_positions"),
]


@pytest.mark.parametrize(("link", "constraint"), LINKS, ids=[c for _, c in LINKS])
class TestEveryLinkStaysInsideItsRun:
    async def test_a_link_to_another_run_is_refused(
        self, db: AsyncSession, rows: Rows, link: Link, constraint: str
    ) -> None:
        first, second = await two_runs(db)
        await violates(db, constraint, lambda: link(rows, first, second))

    async def test_a_paper_row_cannot_link_to_a_backtest_row(
        self, db: AsyncSession, rows: Rows, link: Link, constraint: str
    ) -> None:
        first, _ = await two_runs(db)
        await violates(db, constraint, lambda: link(rows, first, PAPER))

    async def test_a_backtest_row_cannot_link_to_a_paper_row(
        self, db: AsyncSession, rows: Rows, link: Link, constraint: str
    ) -> None:
        first, _ = await two_runs(db)
        await violates(db, constraint, lambda: link(rows, PAPER, first))

    async def test_links_within_one_scope_are_accepted(
        self, db: AsyncSession, rows: Rows, link: Link, constraint: str
    ) -> None:
        first, _ = await two_runs(db)
        assert await link(rows, first, first)
        assert await link(rows, PAPER, PAPER)


class TestTheKeyAndDeletion:
    async def test_the_key_is_derived_for_every_writer_and_cannot_be_tampered_with(
        self, db: AsyncSession, rows: Rows
    ) -> None:
        run, _ = await two_runs(db)
        order = await rows.order(run)
        paper = await rows.order(PAPER)
        keys = dict(
            (
                await db.execute(
                    text("SELECT id, run_key FROM orders WHERE id IN (:a, :b)"),
                    {"a": order, "b": paper},
                )
            ).all()
        )
        assert keys == {order: run[1], paper: 0}
        await db.execute(text("UPDATE orders SET run_key = 999 WHERE id = :id"), {"id": paper})
        assert await scalar(db, "SELECT run_key FROM orders WHERE id = :id", id=paper) == 0

    async def test_null_links_are_still_allowed(self, db: AsyncSession, rows: Rows) -> None:
        run, _ = await two_runs(db)
        order = await rows.order(run)
        assert await rows.fill(run, order, None)
        assert await rows.risk_event(run, None)

    async def test_deleting_a_parent_nulls_or_cascades_exactly_as_before(
        self, db: AsyncSession, rows: Rows
    ) -> None:
        run, _ = await two_runs(db)
        opportunity = await rows.opportunity(run)
        signal = await rows.signal(run, opportunity)
        position = await rows.position(run, opportunity)
        order = await rows.order(run, signal=signal)
        fill = await rows.fill(run, order, position)
        pnl = await rows.pnl(run, position)

        await db.execute(text("DELETE FROM positions WHERE id = :id"), {"id": position})
        assert await scalar(db, "SELECT position_id FROM fills WHERE id = :id", id=fill) is None
        assert await scalar(db, "SELECT run_key FROM fills WHERE id = :id", id=fill) == run[1]
        assert (
            await scalar(db, "SELECT position_id FROM pnl_snapshots WHERE id = :id", id=pnl) is None
        )
        await db.execute(text("DELETE FROM opportunities WHERE id = :id"), {"id": opportunity})
        assert await scalar(db, "SELECT count(*) FROM signals WHERE id = :id", id=signal) == 0
        assert await scalar(db, "SELECT signal_id FROM orders WHERE id = :id", id=order) is None
        await db.execute(text("DELETE FROM orders WHERE id = :id"), {"id": order})
        assert await scalar(db, "SELECT count(*) FROM fills WHERE id = :id", id=fill) == 0
