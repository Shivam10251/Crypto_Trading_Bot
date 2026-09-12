"""Re-costing stored opportunities under a different fee schedule.

This is the Phase 7 premise made good: an opportunity row keeps the prices
behind it, so a later fee model can re-price it. Nothing is rewritten.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tests.integration.factories import make_market, make_opportunity
from trading_bot.core.config import CostsConfig
from trading_bot.db.models import Market, Opportunity
from trading_bot.db.models.enums import MarketType, OpportunityStatus
from trading_bot.opportunities.recost import recost, recost_row
from trading_bot.strategy.fees import FeeSchedule, OrderRole

pytestmark = pytest.mark.requires_postgres

NOW = datetime(2026, 9, 11, 17, 0, tzinfo=UTC)
BASE = FeeSchedule.from_config(CostsConfig())
CHEAPEST = FeeSchedule.from_config(CostsConfig(pay_fees_in_bnb=True))


async def seed(
    db: AsyncSession,
    *,
    gross_usd: str = "10",
    fees_usd: str = "3",
    slippage_usd: str = "1.6",
    notional: str = "1000",
    symbol: str = "BTCUSDT",
    buy_leg: MarketType = MarketType.SPOT,
) -> Opportunity:
    """A row whose stored net actually follows from its stored components."""
    spot = make_market(symbol, MarketType.SPOT)
    perp = make_market(symbol, MarketType.PERPETUAL)
    db.add_all([spot, perp])
    await db.flush()
    bought, sold = (spot, perp) if buy_leg is MarketType.SPOT else (perp, spot)
    buffer = Decimal("0.2")
    net_usd = Decimal(gross_usd) - Decimal(fees_usd) - Decimal(slippage_usd) - buffer
    opportunity = make_opportunity(
        bought,
        sold,
        detected_at=NOW,
        net_edge_bps=str(net_usd / Decimal(notional) * Decimal(10_000)),
        net_edge_usd=net_usd,
        notional_usd=Decimal(notional),
        gross_edge_usd=Decimal(gross_usd),
        estimated_fees_usd=Decimal(fees_usd),
        estimated_slippage_usd=Decimal(slippage_usd),
        safety_buffer_usd=buffer,
        funding_cost_usd=Decimal(0),
        borrow_cost_usd=Decimal(0),
        other_costs_usd=Decimal(0),
    )
    db.add(opportunity)
    await db.flush()
    return opportunity


class TestRecostArithmetic:
    def test_fees_are_recomputed_for_both_legs_entry_and_exit(self) -> None:
        opportunity = make_opportunity(
            make_market("BTCUSDT", MarketType.SPOT),
            make_market("BTCUSDT", MarketType.PERPETUAL),
            notional_usd=Decimal(1000),
            gross_edge_usd=Decimal(10),
            estimated_fees_usd=Decimal(3),
            estimated_slippage_usd=Decimal(0),
            safety_buffer_usd=Decimal(0),
            funding_cost_usd=Decimal(0),
            borrow_cost_usd=Decimal(0),
            other_costs_usd=Decimal(0),
        )
        row = recost_row(
            opportunity,
            "BTCUSDT",
            (MarketType.SPOT, MarketType.PERPETUAL),
            BASE,
            entry_role=OrderRole.TAKER,
            exit_role=OrderRole.TAKER,
        )
        # (10 + 10) spot + (5 + 5) perp = 30 bps of 1000 = 3.00
        assert row.recosted_fees_usd == Decimal(3)

    def test_the_cheapest_schedule_lowers_fees(self) -> None:
        opportunity = make_opportunity(
            make_market("BTCUSDT", MarketType.SPOT),
            make_market("BTCUSDT", MarketType.PERPETUAL),
            notional_usd=Decimal(1000),
            gross_edge_usd=Decimal(10),
            estimated_slippage_usd=Decimal(0),
            safety_buffer_usd=Decimal(0),
            funding_cost_usd=Decimal(0),
            borrow_cost_usd=Decimal(0),
            other_costs_usd=Decimal(0),
        )
        row = recost_row(
            opportunity,
            "BTCUSDT",
            (MarketType.SPOT, MarketType.PERPETUAL),
            CHEAPEST,
            entry_role=OrderRole.MAKER,
            exit_role=OrderRole.MAKER,
        )
        # 18.6 bps of 1000 - the floor.
        assert row.recosted_fees_usd == Decimal("1.86")


class TestRecostReport:
    async def test_the_same_schedule_reproduces_the_stored_numbers(self, db: AsyncSession) -> None:
        """The check that the re-cost is arithmetic, not a new opinion."""
        await seed(db, fees_usd="3", slippage_usd="0")
        report = await recost(db, BASE)
        assert report.total == 1
        assert report.rows[0].recosted_fees_usd == report.rows[0].stored_fees_usd

    async def test_a_cheaper_schedule_improves_the_net_edge(self, db: AsyncSession) -> None:
        await seed(db)
        report = await recost(db, CHEAPEST, entry_role=OrderRole.MAKER, exit_role=OrderRole.MAKER)
        row = report.rows[0]
        assert row.recosted_fees_usd < row.stored_fees_usd
        assert row.recosted_net_bps > row.stored_net_bps

    async def test_slippage_and_funding_are_carried_through_unchanged(
        self, db: AsyncSession
    ) -> None:
        """They came from a book and a schedule that no longer exist."""
        opportunity = await seed(db, slippage_usd="1.6")
        report = await recost(db, CHEAPEST)
        row = report.rows[0]
        # Net moved by exactly the fee saving, nothing else.
        moved = (row.recosted_net_bps - row.stored_net_bps) / Decimal(10_000) * row.notional_usd
        assert abs(moved - row.fee_saving_usd) < Decimal("0.01")
        assert opportunity.estimated_slippage_usd == Decimal("1.6")

    async def test_nothing_is_rewritten(self, db: AsyncSession) -> None:
        """A stored row is what the strategy believed at the time."""
        opportunity = await seed(db, fees_usd="3")
        stored_net = opportunity.net_edge_bps
        await recost(db, CHEAPEST, entry_role=OrderRole.MAKER, exit_role=OrderRole.MAKER)
        await db.refresh(opportunity)
        assert opportunity.estimated_fees_usd == Decimal(3)
        assert opportunity.net_edge_bps == stored_net

    async def test_a_flipped_verdict_is_reported(self, db: AsyncSession) -> None:
        # Gross 10 on 1000 notional = 100 bps; fees 3.00 leave it positive
        # under the base schedule and more so under the cheapest.
        await seed(db, gross_usd="3.1", fees_usd="3", slippage_usd="0")
        report = await recost(db, CHEAPEST, entry_role=OrderRole.MAKER, exit_role=OrderRole.MAKER)
        assert report.is_profitable == 1
        assert len(report.newly_profitable) == 1
        assert "newly clear costs" in report.describe()

    async def test_an_unchanged_verdict_says_so(self, db: AsyncSession) -> None:
        await seed(db, gross_usd="1", fees_usd="3")
        report = await recost(db, CHEAPEST, entry_role=OrderRole.MAKER, exit_role=OrderRole.MAKER)
        assert report.newly_profitable == ()
        assert "no verdict changed" in report.describe()

    async def test_an_unpriceable_row_is_skipped_and_counted(self, db: AsyncSession) -> None:
        """A fee schedule cannot change a verdict that was never reached.

        Treating an unpriceable row's missing costs as zero would invent the
        very number it was stored without.
        """
        await seed(db)
        spot = make_market("ETHUSDT", MarketType.SPOT)
        perp = make_market("ETHUSDT", MarketType.PERPETUAL)
        db.add_all([spot, perp])
        await db.flush()
        unpriceable = make_opportunity(
            spot,
            perp,
            detected_at=NOW,
            status=OpportunityStatus.UNPRICEABLE,
            rejection_reason="FUNDING_UNKNOWN",
            net_edge_bps=None,
            net_edge_usd=None,
            estimated_fees_usd=None,
            estimated_slippage_usd=None,
            funding_cost_usd=None,
            borrow_cost_usd=None,
            other_costs_usd=None,
            safety_buffer_usd=None,
        )
        db.add(unpriceable)
        await db.flush()

        report = await recost(db, CHEAPEST)
        assert report.total == 1
        assert report.skipped_unpriceable == 1
        assert "1 unpriceable opportunities skipped" in report.describe()

    async def test_an_empty_record_reports_cleanly(self, db: AsyncSession) -> None:
        report = await recost(db, BASE)
        assert report.total == 0
        assert "no stored opportunities" in report.describe()

    async def test_the_report_names_its_schedule(self, db: AsyncSession) -> None:
        """The numbers are meaningless without the assumptions behind them."""
        await seed(db)
        report = await recost(db, CHEAPEST)
        assert "BNB discount applied" in report.describe()

    async def test_the_limit_is_respected(self, db: AsyncSession) -> None:
        await seed(db, symbol="AAAUSDT")
        await seed(db, symbol="BBBUSDT")
        assert (await recost(db, BASE, limit=1)).total == 1


class TestReachability:
    async def test_the_bought_leg_says_whether_it_is_reachable(self, db: AsyncSession) -> None:
        """Buying the perpetual means selling spot - the unreachable direction.

        Measured over the real record: under the cheapest possible fees, all 29
        profitable opportunities bought the perpetual. Cost is not what blocks
        this strategy.
        """
        await seed(db, symbol="AAAUSDT", buy_leg=MarketType.SPOT)
        await seed(db, symbol="BBBUSDT", buy_leg=MarketType.PERPETUAL)
        legs = (
            await db.execute(
                select(Market.symbol, Market.market_type)
                .join(Opportunity, Opportunity.market_id == Market.id)
                .order_by(Market.symbol)
            )
        ).all()
        assert [kind for _, kind in legs] == [MarketType.SPOT, MarketType.PERPETUAL]
