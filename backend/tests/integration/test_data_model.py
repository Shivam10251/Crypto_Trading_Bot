"""Data-model behaviour against a real PostgreSQL database.

Covers the guarantees the platform relies on: the traceability chain holds,
invalid market data is refused, duplicate orders and duplicate exchange
messages cannot be stored twice, and NUMERIC values survive a round trip
exactly.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, StatementError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from tests.integration.factories import (
    NOW,
    make_market,
    make_market_data,
    make_opportunity,
    make_order,
    make_signal,
)
from trading_bot.db.models import (
    ExecutionMode,
    Fill,
    Market,
    MarketData,
    MarketTrade,
    MarketType,
    Opportunity,
    OpportunityStatus,
    Order,
    OrderStatus,
    PnlSnapshot,
    Position,
    PositionStatus,
    RiskDecision,
    RiskEvent,
    RiskEventType,
    Side,
    Signal,
    SystemEvent,
    SystemEventType,
)

pytestmark = pytest.mark.requires_postgres


class TestMarketData:
    async def test_round_trips_with_exact_precision(self, db: AsyncSession) -> None:
        """A price must come back bit-for-bit; float would drift here."""
        market = make_market()
        precise = Decimal("100000.123456789012")
        db.add(make_market_data(market, bid=str(precise), ask="100000.123456789013"))
        await db.flush()
        db.expunge_all()

        stored = (await db.execute(select(MarketData))).scalar_one()
        assert stored.bid == precise
        assert stored.bid.as_tuple().exponent == -12

    async def test_records_both_clocks_and_latency(self, db: AsyncSession) -> None:
        db.add(make_market_data(make_market()))
        await db.flush()
        row = (await db.execute(select(MarketData))).scalar_one()
        assert row.local_timestamp > row.exchange_timestamp
        assert row.latency_ms == 18

    @pytest.mark.parametrize(
        ("bid", "ask", "constraint"),
        [
            ("0", "100001", "prices_positive"),
            ("-1", "100001", "prices_positive"),
            ("100002", "100001", "book_not_crossed"),
        ],
    )
    async def test_rejects_invalid_quotes(
        self, db: AsyncSession, bid: str, ask: str, constraint: str
    ) -> None:
        """Bad data is refused by the database, not merely logged."""
        db.add(make_market_data(make_market(), bid=bid, ask=ask))
        with pytest.raises(IntegrityError, match=constraint):
            await db.flush()

    async def test_rejects_negative_sizes(self, db: AsyncSession) -> None:
        db.add(make_market_data(make_market(), bid_size=Decimal("-1")))
        with pytest.raises(IntegrityError, match="sizes_non_negative"):
            await db.flush()

    async def test_equal_bid_and_ask_is_allowed(self, db: AsyncSession) -> None:
        """A locked book is unusual but real; only crossed books are invalid."""
        db.add(make_market_data(make_market(), bid="100000", ask="100000"))
        await db.flush()


class TestDuplicateProtection:
    async def test_same_client_order_id_cannot_be_stored_twice(self, db: AsyncSession) -> None:
        """A retry after a timeout must not create a second order."""
        market = make_market()
        db.add(make_order(market, client_order_id="dup-1"))
        await db.flush()
        db.add(make_order(market, client_order_id="dup-1"))
        with pytest.raises(IntegrityError, match="mode_run_client_order_id"):
            await db.flush()

    async def test_same_id_in_a_different_mode_is_allowed(self, db: AsyncSession) -> None:
        """Paper and live namespaces are independent."""
        market = make_market()
        db.add(make_order(market, client_order_id="dup-2", mode=ExecutionMode.PAPER))
        db.add(make_order(market, client_order_id="dup-2", mode=ExecutionMode.LIVE))
        await db.flush()

    async def test_replayed_trade_message_is_rejected(self, db: AsyncSession) -> None:
        market = make_market()
        for _ in range(2):
            db.add(
                MarketTrade(
                    market=market,
                    exchange_trade_id="t-1",
                    price=Decimal("100000"),
                    quantity=Decimal("0.5"),
                    aggressor_side=Side.BUY,
                    exchange_timestamp=NOW,
                    local_timestamp=NOW,
                )
            )
        with pytest.raises(IntegrityError, match="market_trade_id"):
            await db.flush()


class TestOpportunityStorage:
    async def test_rejected_opportunities_are_kept(self, db: AsyncSession) -> None:
        """The research dataset must not be filtered down to winners."""
        spot = make_market()
        perp = make_market("BTCUSDT", MarketType.PERPETUAL)
        db.add_all(
            [
                make_opportunity(spot, perp, status=OpportunityStatus.EXECUTED),
                make_opportunity(
                    spot,
                    perp,
                    status=OpportunityStatus.REJECTED,
                    net_edge_bps="-2.0",
                    rejection_reason="net edge negative after fees",
                ),
                make_opportunity(spot, perp, status=OpportunityStatus.EXPIRED),
            ]
        )
        await db.flush()

        by_status = dict(
            (
                await db.execute(
                    select(Opportunity.status, func.count()).group_by(Opportunity.status)
                )
            ).all()
        )
        assert by_status == {
            OpportunityStatus.EXECUTED: 1,
            OpportunityStatus.REJECTED: 1,
            OpportunityStatus.EXPIRED: 1,
        }

    async def test_survived_costs_query(self, db: AsyncSession) -> None:
        """ "How many opportunities survived fees?" must be a cheap query."""
        spot = make_market()
        perp = make_market("BTCUSDT", MarketType.PERPETUAL)
        db.add_all(
            [
                make_opportunity(spot, perp, net_edge_bps="3.5"),
                make_opportunity(spot, perp, net_edge_bps="0.4"),
                make_opportunity(spot, perp, net_edge_bps="-1.2"),
            ]
        )
        await db.flush()

        profitable = await db.scalar(
            select(func.count()).select_from(Opportunity).where(Opportunity.net_edge_bps > 0)
        )
        assert profitable == 2

    async def test_uid_is_generated_for_log_correlation(self, db: AsyncSession) -> None:
        opportunity = make_opportunity(make_market())
        db.add(opportunity)
        await db.flush()
        assert opportunity.uid is not None

    async def test_both_legs_must_differ(self, db: AsyncSession) -> None:
        spot = make_market()
        db.add(make_opportunity(spot, spot))
        with pytest.raises(IntegrityError, match="legs_differ"):
            await db.flush()

    async def test_costs_cannot_be_negative(self, db: AsyncSession) -> None:
        db.add(make_opportunity(make_market(), estimated_fees_usd=Decimal("-1")))
        with pytest.raises(IntegrityError, match="costs_non_negative"):
            await db.flush()


class TestOrderConstraints:
    async def test_cannot_fill_more_than_ordered(self, db: AsyncSession) -> None:
        """Over-filling indicates a reconciliation bug; refuse to record it."""
        db.add(
            make_order(
                make_market(),
                quantity=Decimal("0.01"),
                filled_quantity=Decimal("0.02"),
            )
        )
        with pytest.raises(IntegrityError, match="filled_within_quantity"):
            await db.flush()

    async def test_partial_fill_is_valid(self, db: AsyncSession) -> None:
        db.add(
            make_order(
                make_market(),
                quantity=Decimal("0.01"),
                filled_quantity=Decimal("0.004"),
                status=OrderStatus.PARTIALLY_FILLED,
            )
        )
        await db.flush()

    async def test_invalid_status_is_rejected(self, db: AsyncSession) -> None:
        """An unknown status never reaches the table."""
        db.add(make_order(make_market(), status="TOTALLY_FILLED"))
        with pytest.raises(StatementError, match="not among the defined enum values"):
            await db.flush()


class TestPositionConstraints:
    async def test_closed_position_must_record_exit(self, db: AsyncSession) -> None:
        db.add(
            Position(
                market=make_market(),
                mode=ExecutionMode.PAPER,
                strategy="spot_perp_basis",
                side=Side.BUY,
                status=PositionStatus.CLOSED,
                quantity=Decimal("0.01"),
                entry_price=Decimal("100000"),
                entry_notional_usd=Decimal("1000"),
                opened_at=NOW,
                # exit_price and closed_at deliberately missing
            )
        )
        with pytest.raises(IntegrityError, match="closed_position_complete"):
            await db.flush()

    async def test_open_position_needs_no_exit(self, db: AsyncSession) -> None:
        db.add(
            Position(
                market=make_market(),
                mode=ExecutionMode.PAPER,
                strategy="spot_perp_basis",
                side=Side.BUY,
                status=PositionStatus.OPEN,
                quantity=Decimal("0.01"),
                entry_price=Decimal("100000"),
                entry_notional_usd=Decimal("1000"),
                opened_at=NOW,
            )
        )
        await db.flush()


class TestPnlSnapshots:
    async def test_win_rate_must_be_a_ratio(self, db: AsyncSession) -> None:
        db.add(
            PnlSnapshot(
                captured_at=NOW,
                mode=ExecutionMode.PAPER,
                window="1d",
                win_rate=1.5,
            )
        )
        with pytest.raises(IntegrityError, match="win_rate_ratio"):
            await db.flush()

    async def test_trade_splits_cannot_exceed_count(self, db: AsyncSession) -> None:
        db.add(
            PnlSnapshot(
                captured_at=NOW,
                mode=ExecutionMode.PAPER,
                window="1d",
                trade_count=2,
                winning_trades=2,
                losing_trades=1,
            )
        )
        with pytest.raises(IntegrityError, match="trade_splits_within_count"):
            await db.flush()

    async def test_modes_are_stored_separately(self, db: AsyncSession) -> None:
        """Theoretical, paper and live P&L must never be summed together."""
        for mode in (ExecutionMode.THEORETICAL, ExecutionMode.PAPER, ExecutionMode.LIVE):
            db.add(
                PnlSnapshot(
                    captured_at=NOW,
                    mode=mode,
                    window="1d",
                    realized_pnl_usd=Decimal("10"),
                )
            )
        await db.flush()

        paper_total = await db.scalar(
            select(func.sum(PnlSnapshot.realized_pnl_usd)).where(
                PnlSnapshot.mode == ExecutionMode.PAPER
            )
        )
        assert paper_total == Decimal("10")


class TestTraceabilityChain:
    """The central Phase 1 requirement, end to end."""

    async def test_full_chain_can_be_walked_backwards(self, db: AsyncSession) -> None:
        spot = make_market()
        perp = make_market("BTCUSDT", MarketType.PERPETUAL)
        spot_quote = make_market_data(spot, bid="100000", ask="100001")
        perp_quote = make_market_data(perp, bid="100060", ask="100061")
        db.add_all([spot_quote, perp_quote])
        await db.flush()

        opportunity = make_opportunity(
            spot,
            perp,
            market_data_id=spot_quote.id,
            secondary_market_data_id=perp_quote.id,
        )
        db.add(opportunity)
        await db.flush()

        signal = make_signal(opportunity, spot)
        db.add(signal)
        await db.flush()

        risk_event = RiskEvent(
            occurred_at=NOW,
            event_type=RiskEventType.PRE_TRADE_CHECK,
            decision=RiskDecision.APPROVED,
            mode=ExecutionMode.PAPER,
            intent_id="signal:11111111-1111-1111-1111-111111111111",
            signal_id=signal.id,
            strategy="spot_perp_basis",
            reason="within all limits",
            context={"exposure_usd": "1000", "data_age_ms": 18},
        )
        db.add(risk_event)
        await db.flush()

        order = make_order(
            spot,
            signal_id=signal.id,
            risk_event_id=risk_event.id,
            status=OrderStatus.FILLED,
            filled_quantity=Decimal("0.01"),
            average_fill_price=Decimal("100001"),
        )
        db.add(order)
        await db.flush()

        position = Position(
            market=spot,
            opportunity_id=opportunity.id,
            mode=ExecutionMode.PAPER,
            strategy="spot_perp_basis",
            side=Side.BUY,
            status=PositionStatus.OPEN,
            quantity=Decimal("0.01"),
            entry_price=Decimal("100001"),
            entry_notional_usd=Decimal("1000.01"),
            opened_at=NOW,
        )
        db.add(position)
        await db.flush()

        fill = Fill(
            order_id=order.id,
            position_id=position.id,
            mode=ExecutionMode.PAPER,
            price=Decimal("100001"),
            quantity=Decimal("0.01"),
            fee_usd=Decimal("0.10"),
            slippage_bps=Decimal("0.5"),
            is_maker=False,
            filled_at=NOW,
            latency_ms=42,
        )
        db.add(fill)
        db.add(
            PnlSnapshot(
                captured_at=NOW,
                mode=ExecutionMode.PAPER,
                window="session",
                position_id=position.id,
                realized_pnl_usd=Decimal("0.35"),
                fees_usd=Decimal("0.10"),
                trade_count=1,
                winning_trades=1,
            )
        )
        await db.flush()
        db.expunge_all()

        # Walk back: fill -> order -> signal -> opportunity -> market data.
        loaded = (
            await db.execute(
                select(Fill)
                .options(
                    joinedload(Fill.order)
                    .joinedload(Order.signal)
                    .joinedload(Signal.opportunity)
                    .joinedload(Opportunity.market_data)
                )
                .where(Fill.id == fill.id)
            )
        ).scalar_one()

        traced_order = loaded.order
        traced_signal = traced_order.signal
        assert traced_signal is not None
        traced_opportunity = traced_signal.opportunity
        traced_quote = traced_opportunity.market_data

        assert traced_order.risk_event_id == risk_event.id
        assert traced_opportunity.strategy == "spot_perp_basis"
        assert traced_quote is not None
        # The exact quote the decision was made on.
        assert traced_quote.bid == Decimal("100000.000000000000")
        assert traced_quote.ask == Decimal("100001.000000000000")

        # And the P&L attributes back to the same position.
        pnl = (
            await db.execute(select(PnlSnapshot).where(PnlSnapshot.position_id == position.id))
        ).scalar_one()
        assert pnl.realized_pnl_usd == Decimal("0.35000000")

    async def test_risk_rejection_is_recorded_without_an_order(self, db: AsyncSession) -> None:
        """A rejected signal leaves a complete, explainable record."""
        market = make_market()
        opportunity = make_opportunity(market)
        db.add(opportunity)
        await db.flush()
        signal = make_signal(opportunity, market)
        db.add(signal)
        await db.flush()

        db.add(
            RiskEvent(
                occurred_at=NOW,
                event_type=RiskEventType.STALE_DATA,
                decision=RiskDecision.REJECTED,
                mode=ExecutionMode.PAPER,
                intent_id="signal:22222222-2222-2222-2222-222222222222",
                signal_id=signal.id,
                limit_name="max_stale_data_ms",
                limit_value=Decimal("2000"),
                observed_value=Decimal("5400"),
                reason="market data older than the staleness limit",
            )
        )
        await db.flush()

        event = (
            await db.execute(select(RiskEvent).where(RiskEvent.signal_id == signal.id))
        ).scalar_one()
        assert event.decision is RiskDecision.REJECTED
        assert event.observed_value == Decimal("5400")
        orders = (await db.execute(select(func.count()).select_from(Order))).scalar_one()
        assert orders == 0


class TestCascades:
    async def test_deleting_a_market_removes_its_raw_data(self, db: AsyncSession) -> None:
        quote = make_market_data(make_market())
        db.add(quote)
        await db.flush()
        market_id = quote.market_id

        await db.execute(Market.__table__.delete().where(Market.id == market_id))
        remaining = await db.scalar(
            select(func.count()).select_from(MarketData).where(MarketData.market_id == market_id)
        )
        assert remaining == 0

    async def test_deleting_an_opportunity_removes_its_signals(self, db: AsyncSession) -> None:
        market = make_market()
        opportunity = make_opportunity(market)
        db.add(opportunity)
        await db.flush()
        db.add(make_signal(opportunity, market))
        await db.flush()

        await db.execute(Opportunity.__table__.delete().where(Opportunity.id == opportunity.id))
        assert await db.scalar(select(func.count()).select_from(Signal)) == 0

    async def test_market_with_positions_cannot_be_deleted(self, db: AsyncSession) -> None:
        """RESTRICT protects the audit trail from losing its instrument."""
        market = make_market()
        db.add(
            Position(
                market=market,
                mode=ExecutionMode.PAPER,
                strategy="spot_perp_basis",
                side=Side.BUY,
                status=PositionStatus.OPEN,
                quantity=Decimal("0.01"),
                entry_price=Decimal("100000"),
                entry_notional_usd=Decimal("1000"),
                opened_at=NOW,
            )
        )
        await db.flush()

        with pytest.raises(IntegrityError):
            await db.execute(Market.__table__.delete().where(Market.id == market.id))


class TestSystemEvents:
    async def test_records_infrastructure_events_with_context(self, db: AsyncSession) -> None:
        db.add(
            SystemEvent(
                occurred_at=NOW,
                event_type=SystemEventType.WS_DISCONNECTED,
                component="market_data",
                message="websocket closed by peer",
                context={"symbol": "BTCUSDT", "reconnect_attempt": 1},
            )
        )
        await db.flush()
        event = (await db.execute(select(SystemEvent))).scalar_one()
        assert event.context is not None
        assert event.context["symbol"] == "BTCUSDT"

    async def test_data_gaps_are_explainable_after_the_fact(self, db: AsyncSession) -> None:
        db.add_all(
            [
                SystemEvent(
                    occurred_at=NOW,
                    event_type=SystemEventType.DATA_GAP,
                    component="market_data",
                    message="sequence gap detected",
                ),
                SystemEvent(
                    occurred_at=NOW + timedelta(seconds=5),
                    event_type=SystemEventType.WS_RECONNECTED,
                    component="market_data",
                    message="stream resumed",
                ),
            ]
        )
        await db.flush()
        gaps = await db.scalar(
            select(func.count())
            .select_from(SystemEvent)
            .where(SystemEvent.event_type == SystemEventType.DATA_GAP)
        )
        assert gaps == 1
