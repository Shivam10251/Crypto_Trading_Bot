"""The risk engine: every gate, atomicity under concurrency, and fail-closed paths.

Uses an in-memory fake store (mirroring ``RiskEventStore``'s upsert-by-key
semantics) so these run without PostgreSQL; ``tests/integration/test_risk_engine.py``
covers real persistence and idempotency against the unique constraint.
"""

from __future__ import annotations

import asyncio
import dataclasses
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from decimal import Decimal

from tests.unit.test_execution_coordinator import (
    NOW,
    PERP,
    SPOT,
    Adapter,
    opportunity,
    priced_edge,
)
from trading_bot.core.config import ExecutionConfig, RiskConfig
from trading_bot.db.models.enums import (
    ExecutionMode,
    MarketType,
    RiskDecision,
    RiskEventType,
    Side,
)
from trading_bot.execution.account import PaperAccount
from trading_bot.execution.coordinator import ExecutionAttempt, ExecutionCoordinator
from trading_bot.execution.models import ExecutionResult, OrderRequest, RejectionCode
from trading_bot.risk import post_trade
from trading_bot.risk.engine import RiskEngine
from trading_bot.risk.kill_switch import KillSwitchState
from trading_bot.risk.models import PnlSource, RiskEventDraft
from trading_bot.strategy.evidence import (
    ConstraintEvidence,
    FillEvidence,
    LegEvidence,
    MarketEvidence,
    QuoteEvidence,
)
from trading_bot.strategy.models import Leg, Opportunity, Signal


class FakeStore:
    """Mirrors ``RiskEventStore``'s upsert-by-(mode, intent_id, event_type)."""

    def __init__(
        self,
        *,
        fail: bool = False,
        on_persist: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self.fail = fail
        # Fired once, during the first write: the window in which the world
        # can change while an approval is being recorded.
        self._on_persist = on_persist
        self._fired = False
        self.rows: dict[tuple[str, str, str], tuple[int, RiskEventDraft]] = {}
        self.queued: list[RiskEventDraft] = []
        self._next_id = 1
        self.persist_calls = 0

    async def persist(self, draft: RiskEventDraft) -> int | None:
        self.persist_calls += 1
        if self._on_persist is not None and not self._fired:
            self._fired = True
            await self._on_persist()
        if self.fail:
            return None
        key = (draft.mode.value, draft.intent_id, draft.event_type.value)
        existing = self.rows.get(key)
        if existing is not None:
            row_id, _ = existing
            self.rows[key] = (row_id, draft)
            return row_id
        row_id = self._next_id
        self._next_id += 1
        self.rows[key] = (row_id, draft)
        return row_id

    async def persist_kill_switch(self, draft: RiskEventDraft) -> int | None:
        return await self.persist(draft)

    def queue(self, draft: RiskEventDraft) -> None:
        self.queued.append(draft)


class _MutableClock:
    """A clock the test moves, so "time passed" is a step and not a sleep."""

    def __init__(self, start: datetime) -> None:
        self._now = start

    def __call__(self) -> datetime:
        return self._now

    def set(self, when: datetime) -> None:
        self._now = when

    def advance(self, **delta: float) -> Callable[[], Awaitable[None]]:
        """A coroutine factory that moves the clock - for ``FakeStore``."""

        async def _advance() -> None:
            self._now = self._now + timedelta(**delta)

        return _advance


class FakePnl:
    """A realised-P&L source, which Phase 10 will eventually supply for real."""

    def __init__(self, *, daily: Decimal | None = None, consecutive: int | None = None) -> None:
        self._daily = daily
        self._consecutive = consecutive

    def realized_pnl_today_usd(self) -> Decimal | None:
        return self._daily

    def consecutive_losses(self) -> int | None:
        return self._consecutive


class BrokenAdapter:
    """An adapter that fails rather than answering - a venue client outage."""

    async def submit(self, request: OrderRequest) -> ExecutionResult:
        raise RuntimeError("connection reset by peer")

    async def cancel(self, client_order_id: str) -> object:  # pragma: no cover - unused
        raise NotImplementedError

    async def status(self, client_order_id: str) -> object:  # pragma: no cover - unused
        raise NotImplementedError


class SkewedAdapter(Adapter):
    """Both legs fill, but their fill-time books are far apart."""

    async def submit(self, request: OrderRequest) -> ExecutionResult:
        result = await super().submit(request)
        offset = timedelta(seconds=5) if request.ref.market_type is MarketType.PERPETUAL else None
        return dataclasses.replace(
            result,
            book_local_timestamp=NOW + offset if offset else NOW,
        )


class _EmptyResult:
    def scalars(self) -> _EmptyResult:
        return self

    def first(self) -> None:
        return None


class _EmptySession:
    async def execute(self, *_args: object, **_kwargs: object) -> _EmptyResult:
        return _EmptyResult()

    async def __aenter__(self) -> _EmptySession:
        return self

    async def __aexit__(self, *_exc: object) -> bool:
        return False


def _empty_session_factory() -> _EmptySession:
    return _EmptySession()


async def clear_kill_switch(
    store: FakeStore, *, clock: Callable[[], datetime] | None = None
) -> KillSwitchState:
    """A kill switch that has loaded and found no prior decision - the honest
    starting state for a system that has never tripped it."""
    switch = KillSwitchState(
        store,  # type: ignore[arg-type]
        _empty_session_factory,
        mode=ExecutionMode.PAPER,
        clock=clock or (lambda: NOW),
    )
    await switch.load()
    return switch


def _quote(
    age_ms: int, *, bid: str = "100", ask: str = "101", sequence: int | None = 1
) -> QuoteEvidence:
    """A quote whose *timestamp* is genuinely ``age_ms`` old.

    The recorded ``age_ms`` field is set to match, but the engine recomputes
    from ``local_timestamp`` and ignores it - which is the point: a stale
    input cannot present itself as fresh by lying in the field.
    """
    return QuoteEvidence(
        bid=Decimal(bid),
        ask=Decimal(ask),
        bid_size=Decimal(10),
        ask_size=Decimal(10),
        local_timestamp=NOW - timedelta(milliseconds=age_ms),
        exchange_timestamp=NOW - timedelta(milliseconds=age_ms),
        sequence=sequence,
        age_ms=age_ms,
    )


def _leg_evidence(
    leg: Leg,
    *,
    quote_age_ms: int,
    book_age_ms: int,
    book_sequence: int | None = 1,
    quote: QuoteEvidence | None = None,
    quantity: Decimal | None = None,
) -> LegEvidence:
    quantity = quantity if quantity is not None else leg.quantity
    evidence_quote = quote or dataclasses.replace(
        _quote(quote_age_ms),
        bid=leg.reference_price - Decimal("0.5"),
        ask=leg.reference_price + Decimal("0.5"),
    )
    return LegEvidence(
        ref=leg.ref,
        side=leg.side,
        quote=evidence_quote,
        book_sequence=book_sequence,
        book_local_timestamp=NOW - timedelta(milliseconds=book_age_ms),
        book_exchange_timestamp=NOW - timedelta(milliseconds=book_age_ms),
        book_age_ms=book_age_ms,
        entry=FillEvidence(
            side=leg.side,
            requested=quantity,
            filled=quantity,
            average_price=leg.executable_price,
            levels=((leg.executable_price, quantity),),
        ),
        unwind=(
            FillEvidence(
                side=Side.SELL if leg.side is Side.BUY else Side.BUY,
                requested=quantity,
                filled=quantity,
                average_price=leg.unwind_price,
                levels=((leg.unwind_price, quantity),),
            )
            if leg.unwind_price is not None
            else None
        ),
        constraints=ConstraintEvidence.of(None),
    )


def with_evidence(
    opp: Opportunity,
    *,
    quote_age_ms: int = 10,
    book_age_ms: int = 10,
    buy: LegEvidence | None = None,
    sell: LegEvidence | None = None,
) -> Opportunity:
    evidence = MarketEvidence(
        evaluated_at=NOW,
        requested_notional_usd=opp.notional_usd,
        requested_quantity=opp.quantity,
        executable_quantity=opp.quantity,
        common_step_size=None,
        buy=buy or _leg_evidence(opp.buy, quote_age_ms=quote_age_ms, book_age_ms=book_age_ms),
        sell=sell or _leg_evidence(opp.sell, quote_age_ms=quote_age_ms, book_age_ms=book_age_ms),
    )
    return dataclasses.replace(opp, evidence=evidence)


def fresh_signal(
    opp: Opportunity | None = None,
    *,
    quote_age_ms: int = 10,
    book_age_ms: int = 10,
    generated_at: datetime = NOW,
    expires_at: datetime | None = None,
    quantity: str = "1",
    buy: LegEvidence | None = None,
    sell: LegEvidence | None = None,
) -> Signal:
    opp = with_evidence(
        opp or opportunity(quantity=quantity),
        quote_age_ms=quote_age_ms,
        book_age_ms=book_age_ms,
        buy=buy,
        sell=sell,
    )
    priced = priced_edge(opp)
    return Signal(
        strategy=opp.strategy,
        generated_at=generated_at,
        opportunity=opp,
        edge=priced,
        expected_net_edge_bps=priced.net_edge_bps,
        expires_at=expires_at or (generated_at + timedelta(seconds=5)),
    )


def age_funding(signal: Signal, age_ms: int) -> Signal:
    """The same signal with a funding observation that is genuinely old."""
    pricing = signal.edge.pricing
    assert pricing is not None and pricing.funding is not None
    funding = dataclasses.replace(
        pricing.funding, observed_at=NOW - timedelta(milliseconds=age_ms), age_ms=age_ms
    )
    edge = dataclasses.replace(signal.edge, pricing=dataclasses.replace(pricing, funding=funding))
    return dataclasses.replace(signal, edge=edge)


async def build_engine(
    *,
    risk: RiskConfig | None = None,
    execution: ExecutionConfig | None = None,
    store: FakeStore | None = None,
    switch: KillSwitchState | None = None,
    pnl_source: PnlSource | None = None,
    clock: Callable[[], datetime] | None = None,
) -> tuple[RiskEngine, PaperAccount, FakeStore]:
    risk = risk or RiskConfig()
    store = store or FakeStore()
    account = PaperAccount(execution or ExecutionConfig(), risk)
    engine = RiskEngine(
        risk,
        account,
        store,  # type: ignore[arg-type]
        switch or await clear_kill_switch(store),
        # Probes get their own ledger, so nothing they do can reach the
        # actionable account above.
        shadow_account=PaperAccount(execution or ExecutionConfig(), risk),
        pnl_source=pnl_source,
        mode=ExecutionMode.PAPER,
        clock=clock or (lambda: NOW),
    )
    return engine, account, store


class TestApproval:
    async def test_a_clean_signal_is_approved_and_durably_recorded(self) -> None:
        engine, _account, store = await build_engine()
        verdict = await engine.evaluate(
            fresh_signal(), intent_id="signal:1", opportunity_uid=uuid.uuid4(), is_shadow=False
        )
        assert verdict.is_approved
        assert verdict.risk_event_id is not None
        assert store.rows[("PAPER", "signal:1", "PRE_TRADE_CHECK")][0] == verdict.risk_event_id

    async def test_approval_carries_opportunity_provenance_even_without_a_signal_row(self) -> None:
        engine, _account, store = await build_engine()
        uid = uuid.uuid4()
        await engine.evaluate(
            fresh_signal(), intent_id="signal:2", opportunity_uid=uid, is_shadow=False
        )
        _, draft = store.rows[("PAPER", "signal:2", "PRE_TRADE_CHECK")]
        assert draft.opportunity_uid == uid


class TestDataQuality:
    async def test_missing_evidence_is_rejected(self) -> None:
        engine, _account, _store = await build_engine()
        signal = Signal(
            strategy="spot_perp_basis",
            generated_at=NOW,
            opportunity=opportunity(),
            edge=priced_edge(opportunity()),
            expected_net_edge_bps=Decimal(0),
            expires_at=NOW + timedelta(seconds=5),
        )
        verdict = await engine.evaluate(
            signal, intent_id="signal:3", opportunity_uid=None, is_shadow=False
        )
        assert verdict.decision is RiskDecision.REJECTED
        assert verdict.draft.event_type is RiskEventType.INCOMPLETE_MARKET_DATA

    async def test_a_stale_quote_is_rejected(self) -> None:
        engine, _account, _store = await build_engine()
        stale = fresh_signal(quote_age_ms=5000)
        verdict = await engine.evaluate(
            stale, intent_id="signal:4", opportunity_uid=None, is_shadow=False
        )
        assert verdict.decision is RiskDecision.REJECTED
        assert verdict.draft.event_type is RiskEventType.STALE_DATA
        assert verdict.draft.limit_name == "buy_quote_age_ms"

    async def test_a_stale_book_is_rejected(self) -> None:
        engine, _account, _store = await build_engine()
        stale = fresh_signal(book_age_ms=5000)
        verdict = await engine.evaluate(
            stale, intent_id="signal:5", opportunity_uid=None, is_shadow=False
        )
        assert verdict.decision is RiskDecision.REJECTED
        assert verdict.draft.limit_name == "buy_book_age_ms"

    async def test_a_stale_funding_observation_is_rejected(self) -> None:
        engine, _account, _store = await build_engine()
        stale = age_funding(fresh_signal(), 999_999)
        verdict = await engine.evaluate(
            stale, intent_id="signal:6", opportunity_uid=None, is_shadow=False
        )
        assert verdict.decision is RiskDecision.REJECTED
        assert verdict.draft.limit_name == "funding_age_ms"

    async def test_funding_uses_its_slow_feed_limit_not_the_quote_book_limit(self) -> None:
        """A normal observation between 60-second REST polls remains usable."""
        engine, _account, _store = await build_engine()
        normal = age_funding(fresh_signal(), 30_000)

        verdict = await engine.evaluate(
            normal, intent_id="signal:funding-cadence", opportunity_uid=None, is_shadow=False
        )

        assert verdict.is_approved

    async def test_an_expired_signal_is_rejected(self) -> None:
        engine, _account, _store = await build_engine()
        expired = fresh_signal(
            generated_at=NOW - timedelta(seconds=10), expires_at=NOW - timedelta(seconds=1)
        )
        verdict = await engine.evaluate(
            expired, intent_id="signal:7", opportunity_uid=None, is_shadow=False
        )
        assert verdict.decision is RiskDecision.REJECTED
        assert verdict.draft.event_type is RiskEventType.SIGNAL_EXPIRED

    async def test_excessive_decision_latency_is_rejected(self) -> None:
        risk = RiskConfig(max_latency_ms=100)
        engine, _account, _store = await build_engine(risk=risk)
        late = fresh_signal(generated_at=NOW - timedelta(seconds=1))
        verdict = await engine.evaluate(
            late, intent_id="signal:8", opportunity_uid=None, is_shadow=False
        )
        assert verdict.decision is RiskDecision.REJECTED
        assert verdict.draft.event_type is RiskEventType.LATENCY_EXCEEDED

    async def test_excessive_expected_slippage_is_rejected(self) -> None:
        risk = RiskConfig(max_slippage_bps=1)
        engine, _account, _store = await build_engine(risk=risk)
        slippy = Opportunity(
            strategy="spot_perp_basis",
            detected_at=NOW,
            buy=Leg(
                ref=SPOT,
                side=Side.BUY,
                reference_price=Decimal(100),
                executable_price=Decimal(102),  # 200 bps of slippage
                quantity=Decimal(1),
                unwind_price=Decimal(100),
            ),
            sell=Leg(
                ref=PERP,
                side=Side.SELL,
                reference_price=Decimal(101),
                executable_price=Decimal(101),
                quantity=Decimal(1),
                unwind_price=Decimal(101),
            ),
            reference_price=Decimal(100),
            quantity=Decimal(1),
            notional_usd=Decimal(102),
            gross_edge_bps=Decimal(100),
            gross_edge_usd=Decimal(1),
        )
        wide = fresh_signal(slippy)
        verdict = await engine.evaluate(
            wide, intent_id="signal:9", opportunity_uid=None, is_shadow=False
        )
        assert verdict.decision is RiskDecision.REJECTED
        assert verdict.draft.event_type is RiskEventType.SLIPPAGE_EXCEEDED


class TestAccountLimits:
    async def test_order_notional_exceeded_is_rejected_with_the_right_event_type(self) -> None:
        risk = RiskConfig(
            max_order_notional_usd=50, max_position_notional_usd=50, max_total_exposure_usd=50
        )
        engine, _account, _store = await build_engine(risk=risk)
        verdict = await engine.evaluate(
            fresh_signal(), intent_id="signal:10", opportunity_uid=None, is_shadow=False
        )
        assert verdict.decision is RiskDecision.REJECTED
        assert verdict.draft.event_type is RiskEventType.ORDER_SIZE_EXCEEDED
        assert verdict.draft.limit_name == "max_order_notional_usd"

    async def test_gross_exposure_exceeded_is_rejected_with_the_right_event_type(self) -> None:
        risk = RiskConfig(
            max_order_notional_usd=200, max_position_notional_usd=250, max_total_exposure_usd=250
        )
        engine, _account, _store = await build_engine(risk=risk)
        first = await engine.evaluate(
            fresh_signal(), intent_id="signal:11", opportunity_uid=None, is_shadow=False
        )
        assert first.is_approved
        second = await engine.evaluate(
            fresh_signal(), intent_id="signal:12", opportunity_uid=None, is_shadow=False
        )
        assert second.decision is RiskDecision.REJECTED
        assert second.draft.event_type is RiskEventType.EXPOSURE_LIMIT_EXCEEDED

    async def test_insufficient_cash_is_reported_as_insufficient_resources(self) -> None:
        engine, _account, _store = await build_engine(execution=ExecutionConfig(paper_cash_usd=1))
        verdict = await engine.evaluate(
            fresh_signal(), intent_id="signal:13", opportunity_uid=None, is_shadow=False
        )
        assert verdict.decision is RiskDecision.REJECTED
        assert verdict.draft.event_type is RiskEventType.INSUFFICIENT_RESOURCES


class TestConcurrency:
    async def test_concurrent_signals_that_individually_pass_cannot_collectively_exceed_exposure(
        self,
    ) -> None:
        """Each signal's ~201 USD gross fits the 250 USD limit; two together do not."""
        risk = RiskConfig(
            max_order_notional_usd=200, max_position_notional_usd=250, max_total_exposure_usd=250
        )
        engine, _account, _store = await build_engine(risk=risk)
        results = await asyncio.gather(
            *(
                engine.evaluate(
                    fresh_signal(),
                    intent_id=f"signal:concurrent:{i}",
                    opportunity_uid=None,
                    is_shadow=False,
                )
                for i in range(10)
            )
        )
        approved = [v for v in results if v.is_approved]
        rejected = [v for v in results if not v.is_approved]
        assert len(approved) == 1
        assert len(rejected) == 9
        assert all(v.draft.event_type is RiskEventType.EXPOSURE_LIMIT_EXCEEDED for v in rejected)

    async def test_retrying_the_same_intent_is_idempotent(self) -> None:
        engine, account, store = await build_engine()
        first = await engine.evaluate(
            fresh_signal(), intent_id="signal:retry", opportunity_uid=None, is_shadow=False
        )
        gross_after_first = account.gross_exposure_usd
        second = await engine.evaluate(
            fresh_signal(), intent_id="signal:retry", opportunity_uid=None, is_shadow=False
        )
        assert first.risk_event_id == second.risk_event_id
        assert account.gross_exposure_usd == gross_after_first
        assert len(store.rows) == 1  # the second evaluation converges on the same row


class TestFailClosed:
    async def test_an_unrecordable_approval_releases_its_reservation_and_denies(self) -> None:
        store = FakeStore(fail=True)
        engine, account, store = await build_engine(store=store)
        verdict = await engine.evaluate(
            fresh_signal(), intent_id="signal:14", opportunity_uid=None, is_shadow=False
        )
        assert not verdict.is_approved
        assert verdict.draft.event_type is RiskEventType.FAIL_CLOSED
        assert account.gross_exposure_usd == 0  # the reservation was released, not leaked


class TestKillSwitch:
    async def test_an_active_switch_pauses_actionable_signals(self) -> None:
        store = FakeStore()
        switch = await clear_kill_switch(store)
        engine, _account, _store = await build_engine(store=store, switch=switch)
        await switch.trigger(who="test", reason="manual halt")
        verdict = await engine.evaluate(
            fresh_signal(), intent_id="signal:15", opportunity_uid=None, is_shadow=False
        )
        assert verdict.decision is RiskDecision.PAUSED
        assert verdict.draft.event_type is RiskEventType.KILL_SWITCH

    async def test_shadow_probes_are_isolated_from_the_kill_switch(self) -> None:
        store = FakeStore()
        switch = await clear_kill_switch(store)
        engine, account, _store = await build_engine(store=store, switch=switch)
        await switch.trigger(who="test", reason="manual halt")
        verdict = await engine.evaluate(
            fresh_signal(), intent_id="shadow:1", opportunity_uid=None, is_shadow=True
        )
        assert verdict.is_approved
        assert account.gross_exposure_usd == 0  # a probe never mutates real exposure

    async def test_trigger_takes_effect_even_if_the_audit_write_fails(self) -> None:
        store = FakeStore()
        switch = await clear_kill_switch(store)
        store.fail = True
        verdict = await switch.trigger(who="test", reason="halt now")
        assert switch.is_active
        assert verdict.risk_event_id is None

    async def test_rearm_stays_engaged_if_the_audit_write_fails(self) -> None:
        store = FakeStore()
        switch = await clear_kill_switch(store)
        await switch.trigger(who="test", reason="halt")
        store.fail = True
        verdict = await switch.rearm(who="test", reason="all clear")
        assert switch.is_active  # unaudited re-arm must not be trusted
        assert verdict.risk_event_id is None

    async def test_rearm_clears_the_switch_once_durably_recorded(self) -> None:
        store = FakeStore()
        switch = await clear_kill_switch(store)
        await switch.trigger(who="test", reason="halt")
        verdict = await switch.rearm(who="test", reason="all clear")
        assert not switch.is_active
        assert verdict.risk_event_id is not None


class TestLossLimitsAreHonestlyDeferred:
    async def test_deferred_policy_does_not_gate_when_pnl_is_unavailable(self) -> None:
        engine, _account, _store = await build_engine(risk=RiskConfig(daily_loss_policy="deferred"))
        verdict = await engine.evaluate(
            fresh_signal(), intent_id="signal:16", opportunity_uid=None, is_shadow=False
        )
        assert verdict.is_approved

    async def test_fail_closed_policy_rejects_every_signal_without_a_pnl_source(self) -> None:
        risk = RiskConfig(daily_loss_policy="fail_closed")
        engine, _account, _store = await build_engine(risk=risk)
        verdict = await engine.evaluate(
            fresh_signal(), intent_id="signal:17", opportunity_uid=None, is_shadow=False
        )
        assert verdict.decision is RiskDecision.REJECTED
        assert verdict.draft.event_type is RiskEventType.DAILY_LOSS_LIMIT


class TestPostTrade:
    async def _attempt(self, *, hedge_perp: bool) -> tuple[Signal, ExecutionAttempt | None]:
        signal = fresh_signal()
        adapter = Adapter(perpetual=("1" if hedge_perp else "0"))
        coordinator = ExecutionCoordinator(adapter, clock=lambda: NOW)
        return signal, await coordinator.execute(signal, execution_intent_id="attempt:1")

    async def test_a_naked_attempt_pauses_further_actionable_entries(self) -> None:
        store = FakeStore()
        switch = await clear_kill_switch(store)
        engine, _account, _store = await build_engine(store=store, switch=switch)
        signal, attempt = await self._attempt(hedge_perp=False)
        assert attempt is not None and not attempt.is_hedged
        verdict = await engine.evaluate_post_trade(
            attempt, signal, intent_id="attempt:1", opportunity_uid=None
        )
        assert verdict is not None
        assert verdict.decision is RiskDecision.PAUSED
        assert switch.is_active
        gated = await engine.evaluate(
            fresh_signal(), intent_id="signal:after-naked", opportunity_uid=None, is_shadow=False
        )
        assert gated.decision is RiskDecision.PAUSED

    async def test_a_clean_hedged_attempt_produces_no_post_trade_event(self) -> None:
        # A generous slippage bound isolates this test to the hedged/naked
        # question: the fixture's adapter fills both legs at a single fixed
        # price, so realised slippage vs. the strategy's expected price is
        # not itself the thing under test here.
        engine, _account, _store = await build_engine(risk=RiskConfig(max_slippage_bps=100_000))
        signal, attempt = await self._attempt(hedge_perp=True)
        assert attempt is not None and attempt.is_hedged
        verdict = await engine.evaluate_post_trade(
            attempt, signal, intent_id="attempt:2", opportunity_uid=None
        )
        assert verdict is None

    async def test_shadow_attempts_are_never_reviewed_post_trade(self) -> None:
        engine, _account, _store = await build_engine()
        signal = fresh_signal()
        adapter = Adapter(perpetual="0")
        coordinator = ExecutionCoordinator(adapter, clock=lambda: NOW)
        attempt = await coordinator.execute(signal, is_shadow=True, execution_intent_id="shadow:9")
        assert attempt is not None and not attempt.is_hedged
        verdict = await engine.evaluate_post_trade(
            attempt, signal, intent_id="shadow:9", opportunity_uid=None
        )
        assert verdict is None

    async def test_an_adapter_failure_pauses_trading(self) -> None:
        store = FakeStore()
        switch = await clear_kill_switch(store)
        engine, _account, _store = await build_engine(store=store, switch=switch)
        signal = fresh_signal()
        attempt = await ExecutionCoordinator(BrokenAdapter(), clock=lambda: NOW).execute(
            signal, execution_intent_id="attempt:broken"
        )
        assert attempt is not None

        verdict = await engine.evaluate_post_trade(
            attempt, signal, intent_id="attempt:broken", opportunity_uid=None
        )

        assert verdict is not None and verdict.decision is RiskDecision.PAUSED
        assert switch.is_active, "an adapter that failed is not a venue we keep trading through"

    async def test_excessive_realised_slippage_pauses_trading(self) -> None:
        store = FakeStore()
        switch = await clear_kill_switch(store)
        # The fixture adapter fills both legs at 100 against an expected 100
        # and 101, so the sell leg realises ~99 bps of adverse slippage.
        engine, _account, _store = await build_engine(
            risk=RiskConfig(max_slippage_bps=5), store=store, switch=switch
        )
        signal = fresh_signal()
        attempt = await ExecutionCoordinator(Adapter(), clock=lambda: NOW).execute(
            signal, execution_intent_id="attempt:slippy"
        )
        assert attempt is not None and attempt.is_hedged

        verdict = await engine.evaluate_post_trade(
            attempt, signal, intent_id="attempt:slippy", opportunity_uid=None
        )

        assert verdict is not None and verdict.decision is RiskDecision.PAUSED
        assert verdict.draft.event_type is RiskEventType.SLIPPAGE_EXCEEDED
        assert switch.is_active

    async def test_excessive_execution_latency_pauses_trading(self) -> None:
        store = FakeStore()
        switch = await clear_kill_switch(store)
        # The fixture adapter reports 100 ms per leg.
        engine, _account, _store = await build_engine(
            risk=RiskConfig(max_latency_ms=10, max_slippage_bps=100_000),
            store=store,
            switch=switch,
        )
        signal = fresh_signal()
        attempt = await ExecutionCoordinator(Adapter(), clock=lambda: NOW).execute(
            signal, execution_intent_id="attempt:slow"
        )
        assert attempt is not None

        verdict = await engine.evaluate_post_trade(
            attempt, signal, intent_id="attempt:slow", opportunity_uid=None
        )

        assert verdict is not None and verdict.decision is RiskDecision.PAUSED
        assert verdict.draft.event_type is RiskEventType.LATENCY_EXCEEDED
        assert switch.is_active

    async def test_cross_leg_skew_is_recorded_but_does_not_pause(self) -> None:
        """The documented exception: skew is measured, not halted on.

        Skew that actually broke the hedge shows up as naked exposure and
        pauses under that rule instead; skew that did not is a timing
        observation, and halting on it would stop trading for a condition
        with no effect on the book.
        """
        store = FakeStore()
        switch = await clear_kill_switch(store)
        engine, _account, _store = await build_engine(
            risk=RiskConfig(max_slippage_bps=100_000, max_latency_ms=100_000),
            store=store,
            switch=switch,
        )
        signal = fresh_signal()
        coordinator = ExecutionCoordinator(SkewedAdapter(), max_leg_skew_ms=1, clock=lambda: NOW)
        attempt = await coordinator.execute(signal, execution_intent_id="attempt:skew")
        assert attempt is not None and attempt.is_hedged and attempt.timing_violation

        verdict = await engine.evaluate_post_trade(
            attempt, signal, intent_id="attempt:skew", opportunity_uid=None
        )

        assert verdict is not None, "it is still recorded"
        assert verdict.decision is RiskDecision.REJECTED, "recorded, not paused"
        assert not switch.is_active
        assert verdict.draft.limit_name == "max_leg_skew_ms"

    async def test_a_worse_fill_that_crosses_account_limits_pauses_trading(self) -> None:
        """Expected notional passing does not excuse excessive settled notional."""

        class WorsePriceAdapter(Adapter):
            async def submit(self, request: OrderRequest) -> ExecutionResult:
                result = await super().submit(request)
                fill = dataclasses.replace(result.fills[0], price=Decimal("110"))
                return dataclasses.replace(result, fills=(fill,))

        risk = RiskConfig(
            max_order_notional_usd=105,
            max_position_notional_usd=210,
            max_total_exposure_usd=210,
            max_slippage_bps=100_000,
        )
        store = FakeStore()
        switch = await clear_kill_switch(store)
        engine, account, _store = await build_engine(risk=risk, store=store, switch=switch)
        item = fresh_signal()
        approved = await engine.evaluate(
            item, intent_id="signal:worse-fill", opportunity_uid=None, is_shadow=False
        )
        assert approved.is_approved
        attempt = await ExecutionCoordinator(
            WorsePriceAdapter(), account=account, clock=lambda: NOW
        ).execute(
            item,
            execution_intent_id="signal:worse-fill",
            risk_event_id=approved.risk_event_id,
            admission=lambda: engine.admit(
                item,
                intent_id="signal:worse-fill",
                opportunity_uid=None,
                is_shadow=False,
            ),
        )
        assert attempt is not None

        verdict = await engine.evaluate_post_trade(
            attempt, item, intent_id="signal:worse-fill", opportunity_uid=None
        )

        assert verdict is not None and verdict.decision is RiskDecision.PAUSED
        assert switch.is_active
        assert any(
            finding["limit_name"] == "max_total_exposure_usd"
            for finding in verdict.draft.context["findings"]  # type: ignore[index,union-attr]
        )

    async def test_post_trade_slippage_uses_the_same_aggregate_as_the_pre_trade_limit(
        self,
    ) -> None:
        """Defect 13: one definition, before and after - adverse, summed."""
        _engine, _account, _store = await build_engine(risk=RiskConfig(max_slippage_bps=100_000))
        signal = fresh_signal()
        attempt = await ExecutionCoordinator(Adapter(), clock=lambda: NOW).execute(
            signal, execution_intent_id="attempt:agg"
        )
        assert attempt is not None
        realised = post_trade.realised_slippage_bps(attempt)
        per_leg = [
            outcome.result.slippage_bps
            for outcome in attempt.legs
            if outcome.result.slippage_bps is not None
        ]
        assert realised == sum((value for value in per_leg if value > 0), Decimal(0))
        # A leg that filled better than expected cannot net off a leg that
        # filled worse - the same rule Leg.slippage_bps applies pre-trade.
        assert realised >= max(per_leg)


class TestTemporalRechecks:
    async def test_a_recorded_age_cannot_make_a_stale_input_look_fresh(self) -> None:
        """Defect 5: ages are recomputed from timestamps, not read off the row."""
        engine, _account, _store = await build_engine(risk=RiskConfig(max_stale_data_ms=500))
        stale_timestamps = fresh_signal(quote_age_ms=5_000)
        # The evidence claims it is 5s old and it genuinely is; now lie about it.
        lying = dataclasses.replace(
            stale_timestamps.opportunity.evidence.buy.quote,
            age_ms=1,  # type: ignore[union-attr]
        )
        opportunity_with_lie = dataclasses.replace(
            stale_timestamps.opportunity,
            evidence=dataclasses.replace(
                stale_timestamps.opportunity.evidence,  # type: ignore[arg-type]
                buy=dataclasses.replace(
                    stale_timestamps.opportunity.evidence.buy,  # type: ignore[union-attr]
                    quote=lying,
                    book_age_ms=1,
                ),
            ),
        )
        signal = dataclasses.replace(
            stale_timestamps,
            opportunity=opportunity_with_lie,
            edge=dataclasses.replace(stale_timestamps.edge, opportunity=opportunity_with_lie),
        )

        verdict = await engine.evaluate(
            signal, intent_id="signal:lie", opportunity_uid=None, is_shadow=False
        )

        assert verdict.decision is RiskDecision.REJECTED
        assert verdict.draft.event_type is RiskEventType.STALE_DATA

    async def test_data_going_stale_during_persistence_withdraws_the_approval(self) -> None:
        """Defect 5: re-checked once the approval is durable, before returning."""
        clock = _MutableClock(NOW)
        store = FakeStore(on_persist=clock.advance(seconds=10))
        engine, account, _store = await build_engine(
            risk=RiskConfig(max_stale_data_ms=1_000, max_latency_ms=100_000),
            store=store,
            clock=clock,
        )

        verdict = await engine.evaluate(
            fresh_signal(), intent_id="signal:aged", opportunity_uid=None, is_shadow=False
        )

        assert not verdict.is_approved
        assert verdict.draft.event_type is RiskEventType.STALE_DATA
        assert verdict.draft.context is not None
        assert "withdrew_approval_risk_event_id" in verdict.draft.context
        assert account.gross_exposure_usd == 0, "the reservation was released"

    async def test_admit_refuses_a_signal_that_went_stale_after_approval(self) -> None:
        clock = _MutableClock(NOW)
        engine, _account, _store = await build_engine(
            risk=RiskConfig(max_stale_data_ms=1_000, max_latency_ms=100_000), clock=clock
        )
        # A long TTL, so this isolates staleness from the separate expiry gate.
        signal = fresh_signal(expires_at=NOW + timedelta(hours=1))
        approved = await engine.evaluate(
            signal, intent_id="signal:admit", opportunity_uid=None, is_shadow=False
        )
        assert approved.is_approved

        clock.set(NOW + timedelta(seconds=30))
        refusal = await engine.admit(
            signal, intent_id="signal:admit", opportunity_uid=None, is_shadow=False
        )

        assert refusal is not None
        assert refusal.code is RejectionCode.RISK_WITHDRAWN

    async def test_admit_passes_while_everything_still_holds(self) -> None:
        engine, _account, _store = await build_engine()
        signal = fresh_signal()
        await engine.evaluate(signal, intent_id="signal:ok", opportunity_uid=None, is_shadow=False)
        assert (
            await engine.admit(signal, intent_id="signal:ok", opportunity_uid=None, is_shadow=False)
            is None
        )


class TestKillSwitchRaces:
    async def test_a_kill_during_approval_persistence_withdraws_the_approval(self) -> None:
        """Defect 2: the switch flips while the approval is being written."""
        store = FakeStore()
        switch = await clear_kill_switch(store)
        store._on_persist = lambda: switch.trigger(who="test", reason="halted mid-write")
        engine, account, _store = await build_engine(store=store, switch=switch)

        verdict = await engine.evaluate(
            fresh_signal(), intent_id="signal:race", opportunity_uid=None, is_shadow=False
        )

        assert not verdict.is_approved
        assert verdict.decision is RiskDecision.PAUSED
        assert account.gross_exposure_usd == 0, "the reservation was released"

    async def test_admit_refuses_once_the_switch_is_engaged(self) -> None:
        store = FakeStore()
        switch = await clear_kill_switch(store)
        engine, _account, _store = await build_engine(store=store, switch=switch)
        signal = fresh_signal()
        assert (
            await engine.evaluate(
                signal, intent_id="signal:late", opportunity_uid=None, is_shadow=False
            )
        ).is_approved

        await switch.trigger(who="test", reason="halted after approval")
        refusal = await engine.admit(
            signal, intent_id="signal:late", opportunity_uid=None, is_shadow=False
        )

        assert refusal is not None
        assert refusal.code is RejectionCode.RISK_PAUSED

    async def test_a_retried_intent_cannot_reuse_a_reservation_made_before_the_kill(self) -> None:
        """Idempotency must not become a way around the guard."""
        store = FakeStore()
        switch = await clear_kill_switch(store)
        engine, _account, _store = await build_engine(store=store, switch=switch)
        signal = fresh_signal()
        first = await engine.evaluate(
            signal, intent_id="signal:retry-halt", opportunity_uid=None, is_shadow=False
        )
        assert first.is_approved

        await switch.trigger(who="test", reason="halted between attempts")
        second = await engine.evaluate(
            signal, intent_id="signal:retry-halt", opportunity_uid=None, is_shadow=False
        )

        assert not second.is_approved
        assert second.decision is RiskDecision.PAUSED


class TestEvidenceValidation:
    async def _reject_reason(self, signal: Signal, intent: str) -> RiskEventDraft:
        engine, _account, _store = await build_engine()
        verdict = await engine.evaluate(
            signal, intent_id=intent, opportunity_uid=None, is_shadow=False
        )
        assert verdict.decision is RiskDecision.REJECTED
        assert verdict.draft.event_type is RiskEventType.INCOMPLETE_MARKET_DATA
        return verdict.draft

    async def test_evidence_for_another_market_is_refused(self) -> None:
        opp = opportunity()
        wrong = _leg_evidence(
            dataclasses.replace(opp.buy, ref=PERP), quote_age_ms=10, book_age_ms=10
        )
        draft = await self._reject_reason(fresh_signal(buy=wrong), "signal:market")
        assert draft.limit_name == "buy_market"

    async def test_evidence_for_the_wrong_side_is_refused(self) -> None:
        opp = opportunity()
        flipped = _leg_evidence(
            dataclasses.replace(opp.buy, side=Side.SELL), quote_age_ms=10, book_age_ms=10
        )
        draft = await self._reject_reason(fresh_signal(buy=flipped), "signal:side")
        assert draft.limit_name == "buy_side"

    async def test_evidence_priced_at_another_quantity_is_refused(self) -> None:
        opp = opportunity()
        resized = _leg_evidence(opp.buy, quote_age_ms=10, book_age_ms=10, quantity=Decimal("0.5"))
        draft = await self._reject_reason(fresh_signal(buy=resized), "signal:quantity")
        assert draft.limit_name == "buy_quantity"

    async def test_a_crossed_quote_is_refused(self) -> None:
        opp = opportunity()
        crossed = _leg_evidence(
            opp.buy,
            quote_age_ms=10,
            book_age_ms=10,
            quote=_quote(10, bid="101", ask="100"),
        )
        draft = await self._reject_reason(fresh_signal(buy=crossed), "signal:crossed")
        assert draft.limit_name == "buy_quote_crossed"

    async def test_an_unsynchronised_book_is_refused(self) -> None:
        opp = opportunity()
        unsynced = _leg_evidence(opp.buy, quote_age_ms=10, book_age_ms=10, book_sequence=None)
        draft = await self._reject_reason(fresh_signal(buy=unsynced), "signal:sequence")
        assert draft.limit_name == "buy_book_sequence"

    async def test_a_timestamp_from_the_future_is_refused(self) -> None:
        opp = opportunity()
        ahead = _leg_evidence(opp.buy, quote_age_ms=-5_000, book_age_ms=10)
        draft = await self._reject_reason(fresh_signal(buy=ahead), "signal:future")
        assert draft.limit_name == "buy_quote"

    async def test_a_perpetual_leg_without_funding_evidence_is_refused(self) -> None:
        base = fresh_signal()
        pricing = base.edge.pricing
        assert pricing is not None
        stripped = dataclasses.replace(
            base,
            edge=dataclasses.replace(base.edge, pricing=dataclasses.replace(pricing, funding=None)),
        )
        draft = await self._reject_reason(stripped, "signal:funding")
        assert draft.limit_name == "funding_evidence"

    async def test_a_leg_price_that_does_not_match_its_book_walk_is_refused(self) -> None:
        base = fresh_signal()
        evidence = base.opportunity.evidence
        assert evidence is not None
        false_entry = dataclasses.replace(
            evidence.buy.entry,
            average_price=Decimal("1"),
            levels=((Decimal("1"), evidence.buy.entry.filled),),
        )
        false_buy = dataclasses.replace(evidence.buy, entry=false_entry)
        changed_opportunity = dataclasses.replace(
            base.opportunity, evidence=dataclasses.replace(evidence, buy=false_buy)
        )
        changed = dataclasses.replace(
            base,
            opportunity=changed_opportunity,
            edge=dataclasses.replace(base.edge, opportunity=changed_opportunity),
        )

        draft = await self._reject_reason(changed, "signal:false-price")
        assert draft.limit_name == "buy_executable_price"

    async def test_a_future_signal_generation_time_is_refused(self) -> None:
        future = fresh_signal(
            generated_at=NOW + timedelta(seconds=5),
            expires_at=NOW + timedelta(seconds=10),
        )
        draft = await self._reject_reason(future, "signal:future-generation")
        assert draft.limit_name == "signal_generated_at"

    async def test_missing_unwind_walk_is_refused(self) -> None:
        base = fresh_signal()
        evidence = base.opportunity.evidence
        assert evidence is not None
        changed_opportunity = dataclasses.replace(
            base.opportunity,
            evidence=dataclasses.replace(
                evidence, buy=dataclasses.replace(evidence.buy, unwind=None)
            ),
        )
        changed = dataclasses.replace(
            base,
            opportunity=changed_opportunity,
            edge=dataclasses.replace(base.edge, opportunity=changed_opportunity),
        )

        draft = await self._reject_reason(changed, "signal:missing-unwind")
        assert draft.limit_name == "buy_unwind"

    async def test_timezone_naive_expiry_is_refused_without_raising(self) -> None:
        malformed = dataclasses.replace(
            fresh_signal(), expires_at=(NOW + timedelta(seconds=5)).replace(tzinfo=None)
        )
        draft = await self._reject_reason(malformed, "signal:naive-expiry")
        assert draft.limit_name == "expires_at"

    async def test_non_finite_quote_is_refused_without_decimal_error(self) -> None:
        opp = opportunity()
        malformed = _leg_evidence(
            opp.buy,
            quote_age_ms=10,
            book_age_ms=10,
            quote=_quote(10, bid="NaN", ask="101"),
        )
        draft = await self._reject_reason(fresh_signal(buy=malformed), "signal:nan-quote")
        assert draft.limit_name == "buy_quote_price"


class TestLossLimits:
    async def test_an_approval_names_the_controls_it_could_not_evaluate(self) -> None:
        """Defect 7: an approval must never imply a deferred control passed."""
        engine, _account, store = await build_engine()
        verdict = await engine.evaluate(
            fresh_signal(), intent_id="signal:deferred", opportunity_uid=None, is_shadow=False
        )
        assert verdict.is_approved
        context = verdict.draft.context
        assert context is not None
        assert context["deferred_controls"] == ["max_daily_loss_usd", "max_consecutive_losses"]
        assert "max_daily_loss_usd" not in context["enforced"]
        assert "not evaluated" in verdict.draft.reason
        assert store.rows

    async def test_a_daily_loss_breach_halts_trading_for_the_configured_period(self) -> None:
        clock = _MutableClock(NOW)
        store = FakeStore()
        switch = await clear_kill_switch(store, clock=clock)
        engine, _account, _store = await build_engine(
            risk=RiskConfig(max_daily_loss_usd=100, daily_loss_halt_minutes=60),
            store=store,
            switch=switch,
            pnl_source=FakePnl(daily=Decimal(-150)),
            clock=clock,
        )

        verdict = await engine.evaluate(
            fresh_signal(), intent_id="signal:loss", opportunity_uid=None, is_shadow=False
        )

        assert verdict.decision is RiskDecision.PAUSED
        assert verdict.draft.event_type is RiskEventType.DAILY_LOSS_LIMIT
        assert switch.is_active, "trading is disabled, not merely this signal rejected"
        clock.set(NOW + timedelta(minutes=61))
        assert not switch.is_active, "and it expires after the configured period"

    async def test_a_consecutive_loss_breach_pauses_until_someone_rearms(self) -> None:
        clock = _MutableClock(NOW)
        store = FakeStore()
        switch = await clear_kill_switch(store, clock=clock)
        engine, _account, _store = await build_engine(
            risk=RiskConfig(max_consecutive_losses=3),
            store=store,
            switch=switch,
            pnl_source=FakePnl(daily=Decimal(0), consecutive=3),
            clock=clock,
        )

        verdict = await engine.evaluate(
            fresh_signal(), intent_id="signal:streak", opportunity_uid=None, is_shadow=False
        )

        assert verdict.decision is RiskDecision.PAUSED
        assert verdict.draft.event_type is RiskEventType.CONSECUTIVE_LOSSES
        assert switch.is_active
        clock.set(NOW + timedelta(days=7))
        assert switch.is_active, "no timer clears a strategy that stopped working"
        await switch.rearm(who="reviewer", reason="reviewed and resuming")
        assert not switch.is_active


class TestShadowIsolation:
    async def test_a_probe_cannot_consume_actionable_capacity(self) -> None:
        """Defect 9: separate ledgers, so a probe reserves nothing real."""
        risk = RiskConfig(
            max_order_notional_usd=200, max_position_notional_usd=250, max_total_exposure_usd=250
        )
        engine, account, _store = await build_engine(risk=risk)

        probe = await engine.evaluate(
            fresh_signal(), intent_id="shadow:greedy", opportunity_uid=None, is_shadow=True
        )
        actionable = await engine.evaluate(
            fresh_signal(), intent_id="signal:after-probe", opportunity_uid=None, is_shadow=False
        )

        assert probe.is_approved
        assert actionable.is_approved, "the probe consumed none of the real budget"
        assert account.gross_exposure_usd == 0

    async def test_a_probe_is_not_blocked_by_exhausted_actionable_capacity(self) -> None:
        risk = RiskConfig(
            max_order_notional_usd=200, max_position_notional_usd=250, max_total_exposure_usd=250
        )
        engine, _account, _store = await build_engine(risk=risk)
        first = await engine.evaluate(
            fresh_signal(), intent_id="signal:fills-budget", opportunity_uid=None, is_shadow=False
        )
        second = await engine.evaluate(
            fresh_signal(), intent_id="signal:over-budget", opportunity_uid=None, is_shadow=False
        )
        assert first.is_approved and not second.is_approved

        probe = await engine.evaluate(
            fresh_signal(), intent_id="shadow:unaffected", opportunity_uid=None, is_shadow=True
        )

        assert probe.is_approved, "research is not rationed by the trading budget"

    async def test_a_probe_without_its_own_ledger_is_refused_rather_than_borrowing_one(
        self,
    ) -> None:
        store = FakeStore()
        risk = RiskConfig()
        engine = RiskEngine(
            risk,
            PaperAccount(ExecutionConfig(), risk),
            store,  # type: ignore[arg-type]
            await clear_kill_switch(store),
            mode=ExecutionMode.PAPER,
            clock=lambda: NOW,
        )

        verdict = await engine.evaluate(
            fresh_signal(), intent_id="shadow:no-account", opportunity_uid=None, is_shadow=True
        )

        assert not verdict.is_approved
        assert verdict.draft.event_type is RiskEventType.FAIL_CLOSED
