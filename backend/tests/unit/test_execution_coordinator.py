"""Two legs, and what happens when they disagree.

Leg risk was listed as an unmodelled cost in Phase 5 and could not be priced
in Phase 6. These tests are the first place it is a measured outcome rather
than a caveat, so most of them are about the pair failing to agree.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from trading_bot.core.config import CostsConfig, ExecutionConfig, RiskConfig
from trading_bot.db.models.enums import MarketType, OrderStatus, OrderType, Side
from trading_bot.exchange.models import FundingInfo, MarketRef
from trading_bot.execution.account import PaperAccount
from trading_bot.execution.coordinator import ExecutionCoordinator
from trading_bot.execution.models import (
    CancelAck,
    ExecutionResult,
    OrderIntent,
    OrderRequest,
    RejectionCode,
    SimulatedFill,
)
from trading_bot.execution.shadow import choose_probe, probe_signal, reachable
from trading_bot.strategy.costs import TransactionCostModel
from trading_bot.strategy.models import Edge, Leg, Opportunity, RejectionReason, Signal
from trading_bot.strategy.runner import EvaluatedOpportunity, StrategyEvaluation

NOW = datetime(2026, 9, 12, 12, 0, tzinfo=UTC)
SPOT = MarketRef("binance", "BTCUSDT", MarketType.SPOT)
PERP = MarketRef("binance", "BTCUSDT", MarketType.PERPETUAL)
FREE = CostsConfig(
    spot_taker_fee_bps=0.0,
    perp_taker_fee_bps=0.0,
    safety_buffer_bps=0.0,
    spot_borrow_rate_bps_per_day=0.0,
)


def leg(ref: MarketRef, side: Side, price: str = "100", quantity: str = "1") -> Leg:
    return Leg(
        ref=ref,
        side=side,
        reference_price=Decimal(price),
        executable_price=Decimal(price),
        quantity=Decimal(quantity),
        unwind_price=Decimal(price),
    )


def opportunity(*, perp_rich: bool = True, quantity: str = "1") -> Opportunity:
    """Perp rich: buy spot, sell perp - the direction a cash account can reach."""
    buy = leg(SPOT if perp_rich else PERP, Side.BUY, "100", quantity)
    sell = leg(PERP if perp_rich else SPOT, Side.SELL, "101", quantity)
    return Opportunity(
        strategy="spot_perp_basis",
        detected_at=NOW,
        buy=buy,
        sell=sell,
        reference_price=Decimal(100),
        quantity=Decimal(quantity),
        notional_usd=Decimal(100) * Decimal(quantity),
        gross_edge_bps=Decimal(100),
        gross_edge_usd=Decimal(quantity),
    )


FUNDING = FundingInfo(
    ref=PERP,
    mark_price=Decimal(100),
    index_price=Decimal(100),
    last_funding_rate=Decimal(0),
    next_funding_time=NOW + timedelta(hours=4),
    local_timestamp=NOW,
    funding_interval_hours=8,
)


def priced_edge(opp: Opportunity) -> Edge:
    edge = TransactionCostModel(FREE).estimate(opp, FUNDING)
    assert isinstance(edge, Edge), edge
    return edge


def signal(opp: Opportunity | None = None) -> Signal:
    """A live signal for tests that run the coordinator on the real clock.

    ``expires_at`` is relative to *now*, not to the fixed ``NOW`` the prices
    are pinned to. A fixture that expires at a hard-coded instant silently
    rots the moment the wall clock passes it, turning every default-clock
    coordinator test into an expired-signal test.
    """
    opp = opp or opportunity()
    priced = priced_edge(opp)
    return Signal(
        strategy="spot_perp_basis",
        generated_at=NOW,
        opportunity=opp,
        edge=priced,
        expected_net_edge_bps=priced.net_edge_bps,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )


class Adapter:
    """An adapter whose answer per market the test dictates."""

    def __init__(self, **filled: str | None) -> None:
        # symbol:market_type -> quantity filled, or None for "refused".
        self.filled = filled
        self.submitted: list[OrderRequest] = []

    async def submit(self, request: OrderRequest) -> ExecutionResult:
        self.submitted.append(request)
        key = request.ref.market_type.value.lower()
        amount = self.filled.get(key, str(request.quantity))
        quantity = Decimal(amount) if amount is not None else Decimal(0)
        fills = (
            (
                SimulatedFill(
                    price=Decimal(100),
                    quantity=quantity,
                    filled_at=NOW,
                    is_maker=False,
                    fee_usd=Decimal(0),
                ),
            )
            if quantity > 0
            else ()
        )
        if quantity >= request.quantity:
            status, rejection = OrderStatus.FILLED, None
        elif quantity > 0:
            status, rejection = OrderStatus.PARTIALLY_FILLED, RejectionCode.INSUFFICIENT_DEPTH
        else:
            status, rejection = OrderStatus.REJECTED, RejectionCode.NO_LIQUIDITY
        return ExecutionResult(
            request=request,
            status=status,
            fills=fills,
            submitted_at=NOW,
            acknowledged_at=NOW,
            closed_at=NOW,
            latency_ms=100,
            rejection=rejection,
        )

    async def cancel(self, client_order_id: str) -> object:  # pragma: no cover - unused
        raise NotImplementedError

    async def status(self, client_order_id: str) -> object:  # pragma: no cover - unused
        raise NotImplementedError


class TestLegRisk:
    async def test_both_legs_filling_is_hedged(self) -> None:
        attempt = await ExecutionCoordinator(Adapter()).execute(signal())
        assert attempt is not None
        assert attempt.is_hedged
        assert attempt.unhedged_quantity == 0
        assert "hedged" in attempt.describe()

    async def test_one_leg_missing_leaves_naked_exposure(self) -> None:
        """The risk the strategy has always carried and never counted."""
        attempt = await ExecutionCoordinator(Adapter(perpetual=None)).execute(signal())
        assert attempt is not None
        assert not attempt.is_hedged
        assert attempt.unhedged_quantity == Decimal(1)
        assert "UNHEDGED" in attempt.describe()

    async def test_legs_filling_different_amounts_are_naked_for_the_difference(self) -> None:
        adapter = Adapter(spot="1", perpetual="0.4")
        attempt = await ExecutionCoordinator(adapter).execute(signal())
        assert attempt is not None
        assert attempt.unhedged_quantity == Decimal("0.6")

    async def test_neither_leg_filling_is_not_a_trade(self) -> None:
        attempt = await ExecutionCoordinator(Adapter(spot=None, perpetual=None)).execute(signal())
        assert attempt is not None
        assert attempt.is_empty
        assert attempt.describe() == "nothing filled"

    async def test_one_adapter_exception_does_not_erase_the_other_leg(self) -> None:
        class OneLegRaises(Adapter):
            async def submit(self, request: OrderRequest) -> ExecutionResult:
                if request.ref is PERP:
                    raise RuntimeError("venue connection dropped")
                return await super().submit(request)

        attempt = await ExecutionCoordinator(OneLegRaises()).execute(signal())
        assert attempt is not None
        assert attempt.buy.result.status is OrderStatus.FILLED
        assert attempt.sell.result.status is OrderStatus.FAILED
        assert attempt.sell.result.rejection is RejectionCode.ADAPTER_ERROR
        assert attempt.unhedged_quantity == Decimal(1)


class TestPlacement:
    async def test_both_legs_are_submitted(self) -> None:
        adapter = Adapter()
        await ExecutionCoordinator(adapter).execute(signal())
        assert {r.ref for r in adapter.submitted} == {SPOT, PERP}
        assert all(r.intent is OrderIntent.OPEN for r in adapter.submitted)

    async def test_kill_cancellation_tracks_every_concurrent_attempt(self) -> None:
        """One worker finishing must not erase another worker's open orders."""

        class BlockingAdapter(Adapter):
            def __init__(self) -> None:
                super().__init__()
                self.started: list[str] = []
                self.cancelled: list[str] = []
                self.all_started = asyncio.Event()
                self.release = asyncio.Event()

            async def submit(self, request: OrderRequest) -> ExecutionResult:
                self.started.append(request.client_order_id)
                if len(self.started) == 4:
                    self.all_started.set()
                await self.release.wait()
                return await super().submit(request)

            async def cancel(self, client_order_id: str) -> CancelAck:
                self.cancelled.append(client_order_id)
                return CancelAck(client_order_id, True, NOW)

        adapter = BlockingAdapter()
        coordinator = ExecutionCoordinator(adapter)
        first = asyncio.create_task(coordinator.execute(signal(), execution_intent_id="first"))
        second = asyncio.create_task(coordinator.execute(signal(), execution_intent_id="second"))
        await adapter.all_started.wait()

        assert await coordinator.cancel_in_flight("kill") == 4
        assert set(adapter.cancelled) == set(adapter.started)

        adapter.release.set()
        await asyncio.gather(first, second)

    async def test_each_leg_carries_the_price_the_strategy_expected(self) -> None:
        """Realised slippage is measured against this, so it has to be there."""
        adapter = Adapter()
        await ExecutionCoordinator(adapter).execute(signal())
        assert all(r.expected_price is not None for r in adapter.submitted)

    async def test_order_ids_are_deterministic_per_attempt(self) -> None:
        """A retry of the same attempt must not open a second position."""
        adapter = Adapter()
        attempt = await ExecutionCoordinator(adapter).execute(signal())
        assert attempt is not None
        ids = [r.client_order_id for r in adapter.submitted]
        assert ids == [f"{attempt.attempt_id}-0", f"{attempt.attempt_id}-1"]
        assert len(set(ids)) == 2

    async def test_retrying_the_same_intent_reuses_both_order_ids(self) -> None:
        adapter = Adapter()
        coordinator = ExecutionCoordinator(adapter)
        first = await coordinator.execute(signal(), execution_intent_id="signal:episode-1")
        first_ids = [request.client_order_id for request in adapter.submitted]
        adapter.submitted.clear()
        second = await coordinator.execute(signal(), execution_intent_id="signal:episode-1")
        assert first is not None and second is not None
        assert first.attempt_id == second.attempt_id
        assert [request.client_order_id for request in adapter.submitted] == first_ids

    async def test_expired_signal_is_audited_without_reaching_the_adapter(self) -> None:
        adapter = Adapter()
        # Past the fixture's real-clock-relative TTL, whenever this runs.
        coordinator = ExecutionCoordinator(
            adapter, clock=lambda: datetime.now(UTC) + timedelta(hours=2)
        )
        attempt = await coordinator.execute(signal())
        assert attempt is not None
        assert adapter.submitted == []
        assert all(
            outcome.result.rejection is RejectionCode.SIGNAL_EXPIRED for outcome in attempt.legs
        )

    async def test_a_limit_entry_rests_at_the_strategys_own_price(self) -> None:
        adapter = Adapter()
        coordinator = ExecutionCoordinator(adapter, order_type=OrderType.LIMIT)
        await coordinator.execute(signal())
        assert all(r.order_type is OrderType.LIMIT for r in adapter.submitted)
        assert all(r.price is not None for r in adapter.submitted)

    async def test_a_limit_is_rounded_to_a_tick_the_venue_accepts(self) -> None:
        """Measured live: 10 of 40 limit orders were rejected without this.

        The strategy's executable price is a VWAP of walking the book, which
        is almost never a multiple of the tick. Worse, it was rejected on one
        leg and not the other, leaving the attempt half-filled and naked.
        """
        from trading_bot.exchange.models import MarketSpec

        def spec(ref: MarketRef) -> MarketSpec:
            return MarketSpec(
                ref=ref,
                base_asset="BTC",
                quote_asset="USDT",
                is_active=True,
                tick_size=Decimal("0.10"),
            )

        adapter = Adapter()
        awkward = Opportunity(
            strategy="spot_perp_basis",
            detected_at=NOW,
            buy=leg(SPOT, Side.BUY, "100.07"),
            sell=leg(PERP, Side.SELL, "101.03"),
            reference_price=Decimal("100.07"),
            quantity=Decimal(1),
            notional_usd=Decimal("100.07"),
            gross_edge_bps=Decimal(96),
            gross_edge_usd=Decimal("0.96"),
        )
        coordinator = ExecutionCoordinator(adapter, order_type=OrderType.LIMIT, specs=spec)
        await coordinator.execute(signal(awkward))
        by_side = {r.side: r for r in adapter.submitted}
        # Rounded AGAINST us on both sides, never to a price nobody offered.
        assert by_side[Side.BUY].price == Decimal("100.00")
        assert by_side[Side.SELL].price == Decimal("101.10")

    async def test_without_a_tick_the_strategys_price_is_used_unchanged(self) -> None:
        adapter = Adapter()
        coordinator = ExecutionCoordinator(adapter, order_type=OrderType.LIMIT)
        await coordinator.execute(signal())
        assert all(r.price is not None for r in adapter.submitted)

    async def test_selling_spot_is_refused_at_the_execution_boundary_too(self) -> None:
        """The strategy's gate, restated where the order would really go.

        Defending a rule in one place only is how it eventually gets past.
        """
        adapter = Adapter()
        cheap = opportunity(perp_rich=False)  # sell spot, buy perp
        attempt = await ExecutionCoordinator(adapter).execute(signal(cheap))
        assert attempt is not None
        assert adapter.submitted == []
        assert all(
            outcome.result.rejection is RejectionCode.BORROW_UNAVAILABLE for outcome in attempt.legs
        )

    async def test_selling_spot_is_allowed_when_configured(self) -> None:
        adapter = Adapter()
        execution = ExecutionConfig(
            paper_allow_margin_borrow=True,
            paper_max_borrow_usd=1000,
        )
        coordinator = ExecutionCoordinator(
            adapter,
            allow_spot_short=True,
            account=PaperAccount(execution, RiskConfig()),
        )
        attempt = await coordinator.execute(signal(opportunity(perp_rich=False)))
        assert attempt is not None


def evaluated(opp: Opportunity, *, priced: bool = True) -> EvaluatedOpportunity:
    return EvaluatedOpportunity(
        opportunity=opp,
        edge=priced_edge(opp) if priced else None,
        signal=None,
        validation=None,
        rejection=RejectionReason.BELOW_MIN_EDGE,
    )


def evaluation(*items: EvaluatedOpportunity) -> StrategyEvaluation:
    return StrategyEvaluation(
        strategy="spot_perp_basis", evaluated_at=NOW, opportunities=tuple(items)
    )


class TestShadowProbes:
    def test_the_best_reachable_opportunity_is_chosen(self) -> None:
        thin = evaluated(opportunity(quantity="1"))
        wide = evaluated(
            Opportunity(
                strategy="spot_perp_basis",
                detected_at=NOW,
                buy=leg(SPOT, Side.BUY, "100"),
                sell=leg(PERP, Side.SELL, "105"),
                reference_price=Decimal(100),
                quantity=Decimal(1),
                notional_usd=Decimal(100),
                gross_edge_bps=Decimal(500),
                gross_edge_usd=Decimal(5),
            )
        )
        chosen = choose_probe([evaluation(thin, wide)], allow_spot_short=False)
        assert chosen is not None
        assert chosen[1] is wide

    def test_a_direction_the_account_cannot_reach_is_never_probed(self) -> None:
        """Probing it would measure the refusal we already know about."""
        cheap = evaluated(opportunity(perp_rich=False))
        assert not reachable(cheap, allow_spot_short=False)
        assert choose_probe([evaluation(cheap)], allow_spot_short=False) is None
        assert choose_probe([evaluation(cheap)], allow_spot_short=True) is not None

    def test_an_unpriceable_opportunity_is_never_probed(self) -> None:
        """With no modelled cost there is nothing to compare a fill against."""
        unpriced = evaluated(opportunity(), priced=False)
        assert choose_probe([evaluation(unpriced)], allow_spot_short=False) is None

    def test_nothing_to_probe_is_not_an_error(self) -> None:
        assert choose_probe([], allow_spot_short=False) is None

    def test_an_actionable_signal_is_never_selected_as_a_shadow_probe(self) -> None:
        active_signal = signal()
        item = EvaluatedOpportunity(
            opportunity=active_signal.opportunity,
            edge=active_signal.edge,
            signal=active_signal,
            validation=None,
        )
        assert choose_probe([evaluation(item)], allow_spot_short=False) is None

    def test_a_probe_signal_carries_the_rejected_opportunity_unchanged(self) -> None:
        item = evaluated(opportunity())
        probe = probe_signal(evaluation(item), item, NOW, timedelta(seconds=5))
        assert probe is not None
        assert probe.opportunity is item.opportunity
        assert probe.expires_at == NOW + timedelta(seconds=5)

    async def test_a_probe_is_flagged_so_it_never_counts_as_a_strategy_trade(self) -> None:
        attempt = await ExecutionCoordinator(Adapter()).execute(signal(), is_shadow=True)
        assert attempt is not None
        assert attempt.is_shadow is True


class TestExecutionPanel:
    async def test_the_panel_leads_with_unhedged_and_explains_each_leg(self) -> None:
        """A screen that only showed successes would hide the finding."""
        from trading_bot.monitoring.execution_view import render_execution

        missed = await ExecutionCoordinator(Adapter(perpetual=None)).execute(signal())
        assert missed is not None
        panel = render_execution([missed])
        assert "UNHEDGED" in panel
        assert "no liquidity" in panel
        assert "rejected" in panel

    async def test_a_probe_is_labelled_on_screen_as_well_as_in_the_row(self) -> None:
        from trading_bot.monitoring.execution_view import render_execution

        probe = await ExecutionCoordinator(Adapter()).execute(signal(), is_shadow=True)
        assert probe is not None
        panel = render_execution([probe])
        assert "shadow" in panel
        assert "not strategy trades" in panel

    async def test_the_panel_trims_rather_than_scrolling_the_market_table_away(self) -> None:
        from trading_bot.monitoring.execution_view import render_execution

        attempts = []
        for _ in range(5):
            attempt = await ExecutionCoordinator(Adapter()).execute(signal())
            assert attempt is not None
            attempts.append(attempt)
        panel = render_execution(attempts, max_rows=4)
        assert "6 more legs" in panel
