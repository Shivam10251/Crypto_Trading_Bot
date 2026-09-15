"""Closing a basis attempt: the orders, the record, and the account.

Phase 8 opened positions and Phase 9 gated them; nothing closed them. The
gross edge a basis attempt is entered on is a *convergence* edge, so until
something exits, "realised P&L" has nothing to be realised from.

Six properties this module exists to hold, each of which is a way closing
goes wrong:

**Both legs at once.** The exits are submitted concurrently, exactly as the
entries were. Sending one and waiting would give the second leg the first
one's latency, and a hedge that unwinds in sequence is unhedged in between.

**Reduce-only, three times over.** The quantity asked for is the position's
own remaining open size; ``RiskEngine.evaluate_exit`` re-derives that
independently and refuses anything that is not strictly reducing; and
``positions.closed_quantity <= quantity`` is a database constraint. A close
can neither increase a position, reverse it, nor open new exposure.

**A halt does not stop a close.** A kill switch blocks new entries. Closing
reduces exposure, so it is deliberately outside that gate - see
``trading_bot.risk.position_exit`` for why, and note that this is not a
general bypass: every other risk check a close is subject to still runs.

**One transaction.** The close's orders, its fills and the position rows they
settle commit together. A crash anywhere leaves either all of it or none of
it, and a retry recomputes the same answer from the same fills.

**A failed leg is real naked exposure.** If one leg closes and the other does
not, the pair is left genuinely unhedged - that is reported to the risk
engine's post-trade review the same way an unhedged *entry* is, and the
residual is picked up as ``UNPAIRED_RESIDUAL`` on the next pass.

**The account is reduced, not re-grown.** ``PaperAccount.settle_exit`` gives
back exactly the entry notional of the quantity closed, so gross exposure
falls as positions close instead of accumulating forever.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from trading_bot.core.config import ExitPolicyConfig
from trading_bot.core.logging import get_logger
from trading_bot.db.models import Fill as FillRow
from trading_bot.db.models import Order as OrderRow
from trading_bot.db.models.enums import ExecutionMode
from trading_bot.execution.account import ExitSettlement, PaperAccount
from trading_bot.execution.base import ExecutionAdapter
from trading_bot.execution.models import (
    ExecutionResult,
    OrderRequest,
)
from trading_bot.portfolio import exits
from trading_bot.portfolio.close_record import (
    close_request,
    failed_result,
    fill_values,
    left_unhedged,
    order_values,
)
from trading_bot.portfolio.records import AttemptRecord, LegRecord
from trading_bot.portfolio.store import (
    ClaimLost,
    PortfolioStore,
    SessionFactory,
    reconcile_within,
)
from trading_bot.portfolio.valuation import ExecutableExit, MarkReader, exit_side
from trading_bot.risk.engine import RiskEngine
from trading_bot.risk.position_exit import ExitLeg, ExitRequest

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class CloseOutcome:
    """What one close attempt did. Every field is observed, none assumed."""

    attempt_id: str
    intent_id: str | None
    reason: exits.ExitReason | None
    submitted: int = 0
    closed_positions: tuple[int, ...] = ()
    filled_quantity: Decimal = Decimal(0)
    #: True when the close left one leg flat and another not: naked exposure.
    left_unhedged: bool = False
    refused: str | None = None

    @property
    def acted(self) -> bool:
        return self.submitted > 0


class PositionCloser:
    """Evaluates the exit policy and, when it fires, closes an attempt."""

    def __init__(
        self,
        *,
        store: PortfolioStore,
        session_factory: SessionFactory,
        adapter: ExecutionAdapter,
        risk: RiskEngine,
        marks: MarkReader,
        account: PaperAccount | None,
        config: ExitPolicyConfig,
        venue: str,
        mode: ExecutionMode = ExecutionMode.PAPER,
        clock: Any = None,
        worker_id: str | None = None,
    ) -> None:
        self._store = store
        self._session_factory = session_factory
        self._adapter = adapter
        self._risk = risk
        self._marks = marks
        self._account = account
        self._config = config
        self._venue = venue
        self._mode = mode
        self._clock = clock or (lambda: datetime.now(UTC))
        # The claim id written to ``positions.close_claim_id``. Random for a
        # service process; a replay names it so reruns write identical rows.
        self._worker_id = worker_id or uuid.uuid4().hex[:32]
        if store.mode is not mode:
            raise ValueError(f"closer mode {mode.value} disagrees with its store's")
        self._scope = store.scope
        self.closes = 0
        self.refusals = 0
        self.unhedged_closes = 0
        # Attempts a sweep could not evaluate. The service keeps sweeping; a
        # backtest fails the run, because a skipped exit is a result no real
        # run would have had.
        self.failures = 0
        self.last_error: str | None = None

    async def run(self) -> None:
        while True:
            await asyncio.sleep(self._config.evaluate_interval_ms / 1000)
            try:
                await self.sweep()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # one bad pass must not stop the loop
                logger.exception("portfolio.exit_sweep_failed", error=str(exc))

    async def sweep(self) -> list[CloseOutcome]:
        """One pass: recover interrupted closes, then evaluate every attempt."""
        now = self._clock()
        attempts = await self._store.live_attempts(self._venue)
        outcomes: list[CloseOutcome] = []
        for attempt in attempts:
            try:
                # Recover first, always. A claim left behind by a crash may
                # already have fills the database has not accounted for, and
                # deciding anything before reading them would decide on a
                # stale open quantity.
                await self._recover(attempt, now)
                if attempt.is_claimed:
                    refreshed = await self._store.attempt(attempt.attempt_id, self._venue)
                    if refreshed is None:
                        continue
                    attempt = refreshed
                outcome = await self.close_if_due(attempt, now=now)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # One attempt that cannot be closed must not stop the others
                # from being evaluated - the next one may be the naked leg.
                self.failures += 1
                self.last_error = f"{attempt.attempt_id}: {type(exc).__name__}: {exc}"
                logger.exception(
                    "portfolio.exit_failed", attempt=attempt.attempt_id, error=str(exc)
                )
                continue
            if outcome is not None:
                outcomes.append(outcome)
        return outcomes

    async def _recover(self, attempt: AttemptRecord, now: datetime) -> None:
        """Re-account any claimed attempt from its durable fills.

        Cheap and idempotent, so it runs on every claimed attempt rather than
        only on ones that look stale: recomputing from fills is the same
        operation whether the close finished, half finished or never started.
        """
        if not attempt.is_claimed:
            return
        closed = await self._store.reconcile([leg.position_id for leg in attempt.legs], now=now)
        if closed:
            logger.info("portfolio.exit_recovered", attempt=attempt.attempt_id, closed=len(closed))

    # --- one attempt ----------------------------------------------------

    def view(self, attempt: AttemptRecord, now: datetime) -> exits.BasisView:
        """Price both legs' exits against the books as they are right now."""
        priced: dict[int, ExecutableExit] = {}
        for leg in attempt.live_legs:
            priced[leg.position_id] = self._marks.executable_exit(
                leg.ref, entry_side=leg.side, quantity=leg.open_quantity
            )
        buy, sell = attempt.buy_leg, attempt.sell_leg
        return exits.BasisView(
            attempt_id=attempt.attempt_id,
            opened_at=min(leg.opened_at for leg in attempt.legs),
            buy_entry_price=buy.entry_price if buy is not None else None,
            sell_entry_price=sell.entry_price if sell is not None else None,
            buy_exit=priced.get(buy.position_id) if buy is not None else None,
            sell_exit=priced.get(sell.position_id) if sell is not None else None,
            is_unpaired=attempt.is_unpaired,
            close_attempts=attempt.close_attempts,
        )

    async def close_if_due(
        self, attempt: AttemptRecord, *, now: datetime | None = None
    ) -> CloseOutcome | None:
        """Evaluate the exit policy for one attempt and act on its answer."""
        moment = now or self._clock()
        live = attempt.live_legs
        if not live:
            return None
        decision = exits.evaluate(self.view(attempt, moment), self._config, moment)
        if not decision.should_close:
            if decision.reason is not None:
                logger.info(
                    "portfolio.exit_deferred",
                    attempt=attempt.attempt_id,
                    reason=decision.reason.value,
                    deferral=decision.deferral.value if decision.deferral else None,
                    detail=decision.detail,
                )
            return None
        assert decision.reason is not None
        return await self.close(attempt, decision, now=moment)

    async def close(
        self, attempt: AttemptRecord, decision: exits.ExitDecision, *, now: datetime
    ) -> CloseOutcome:
        """Claim fresh rows, ask risk, submit both legs, record, settle."""
        assert decision.reason is not None
        reason = decision.reason
        sequence = attempt.close_attempts
        intent_id = f"close:{attempt.attempt_id}:{sequence}"
        try:
            claimed = await self._store.claim(
                attempt,
                reason=reason.value,
                now=now,
                claim_timeout=timedelta(milliseconds=self._config.claim_timeout_ms),
                claim_id=self._worker_id,
            )
        except ClaimLost:
            logger.info("portfolio.exit_claim_lost", attempt=attempt.attempt_id)
            return CloseOutcome(
                attempt_id=attempt.attempt_id,
                intent_id=intent_id,
                reason=reason,
                refused="another worker holds this attempt",
            )
        if claimed is None:  # pragma: no cover - live_legs is non-empty here
            return None  # type: ignore[return-value]

        # The claim returns the locked database view. Never construct a close
        # from the pre-claim object: a concurrent partial reconciliation may
        # have reduced its open quantity while policy was being evaluated.
        fresh = claimed.attempt
        live = fresh.live_legs
        legs = [
            ExitLeg(
                position_id=leg.position_id,
                ref=leg.ref,
                entry_side=leg.side,
                close_side=exit_side(leg.side),
                quantity=leg.open_quantity,
                open_quantity=leg.open_quantity,
                status=leg.status,
                mode=leg.mode,
            )
            for leg in live
        ]
        verdict = await self._risk.evaluate_exit(
            ExitRequest(
                attempt_id=fresh.attempt_id,
                intent_id=claimed.intent_id,
                strategy=fresh.strategy,
                reason=reason.value,
                legs=tuple(legs),
                policy_context=decision.context(),
                mode=self._mode,
            )
        )
        if not verdict.is_approved:
            await self._store.release_claim(
                [leg.position_id for leg in live], claim_id=claimed.claim_id, now=now
            )
            self.refusals += 1
            logger.error(
                "portfolio.exit_refused",
                attempt=fresh.attempt_id,
                reason=reason.value,
                detail=verdict.reason,
            )
            return CloseOutcome(
                attempt_id=fresh.attempt_id,
                intent_id=claimed.intent_id,
                reason=reason,
                refused=verdict.reason,
            )

        try:
            await self._store.confirm_submission(claimed)
        except ClaimLost:
            await self._store.release_claim(
                [leg.position_id for leg in live], claim_id=claimed.claim_id, now=now
            )
            logger.info("portfolio.exit_claim_lost", attempt=fresh.attempt_id)
            return CloseOutcome(
                attempt_id=fresh.attempt_id,
                intent_id=claimed.intent_id,
                reason=reason,
                refused="claim changed before submission",
            )

        requests = [
            close_request(leg, claimed.intent_id, index, fresh.strategy, verdict.risk_event_id)
            for index, leg in enumerate(live)
        ]
        # Concurrently, for the same reason entries are: unwinding one leg
        # before the other leaves the pair unhedged for the gap between them.
        raw = await asyncio.gather(
            *(self._adapter.submit(request) for request in requests), return_exceptions=True
        )
        results = [
            value if isinstance(value, ExecutionResult) else failed_result(request, value, now)
            for request, value in zip(requests, raw, strict=True)
        ]
        closed = await self._record(live, requests, results, fresh, claimed.intent_id, now)
        await self._settle(live, results)
        await self._store.release_claim(
            [leg.position_id for leg in live], claim_id=claimed.claim_id, now=now
        )
        filled = sum((result.filled_quantity for result in results), Decimal(0))
        unhedged = left_unhedged(live, results)
        self.closes += 1
        if unhedged:
            self.unhedged_closes += 1
            # The same condition an unhedged entry produces, and the same
            # response: a durable finding, and a pause on new entries while
            # the book carries a leg with no hedge. Closing the residual is
            # deliberately still allowed - see risk.position_exit.
            await self._risk.record_exit_residual(
                attempt_id=fresh.attempt_id,
                intent_id=claimed.intent_id,
                strategy=fresh.strategy,
                residual={
                    str(leg.ref): str(leg.open_quantity - result.filled_quantity)
                    for leg, result in zip(live, results, strict=True)
                    if leg.open_quantity - result.filled_quantity > 0
                },
            )
        logger.info(
            "portfolio.exit_submitted",
            attempt=attempt.attempt_id,
            reason=reason.value,
            intent=claimed.intent_id,
            legs=len(requests),
            filled=str(filled),
            closed_positions=len(closed),
            unhedged=unhedged,
        )
        return CloseOutcome(
            attempt_id=attempt.attempt_id,
            intent_id=claimed.intent_id,
            reason=reason,
            submitted=len(requests),
            closed_positions=tuple(closed),
            filled_quantity=filled,
            left_unhedged=unhedged,
        )

    # --- durability ------------------------------------------------------

    async def _record(
        self,
        legs: Sequence[LegRecord],
        requests: Sequence[OrderRequest],
        results: Sequence[ExecutionResult],
        attempt: AttemptRecord,
        intent_id: str,
        now: datetime,
    ) -> list[int]:
        """Orders, fills and the positions they settle - one transaction.

        Upserts throughout, so a retry of the same close converges on the rows
        already written instead of duplicating an order or double-counting a
        fill: the close's identity is deterministic, and both unique
        constraints (``mode, client_order_id`` and ``order_id, fill_index``)
        are what make that convergence a guarantee rather than a hope.
        """
        async with self._session_factory() as session:
            run_id = self._scope.backtest_run_id
            values = [
                order_values(leg, request, result, attempt, intent_id, index, run_id)
                for index, (leg, request, result) in enumerate(
                    zip(legs, requests, results, strict=True)
                )
            ]
            statement = insert(OrderRow).values(values)
            await session.execute(
                statement.on_conflict_do_update(
                    constraint="mode_run_client_order_id",
                    set_={
                        key: getattr(statement.excluded, key)
                        for key in values[0]
                        if key not in {"mode", "backtest_run_id", "client_order_id"}
                    },
                )
            )
            stored = await session.execute(
                select(OrderRow.id, OrderRow.client_order_id).where(
                    *self._scope.filters(OrderRow.mode, OrderRow.backtest_run_id),
                    OrderRow.client_order_id.in_([row["client_order_id"] for row in values]),
                )
            )
            order_ids = {client_id: order_id for order_id, client_id in stored}
            fills: list[dict[str, Any]] = []
            for leg, request, result in zip(legs, requests, results, strict=True):
                order_id = order_ids[request.client_order_id]
                fills.extend(fill_values(leg, result, order_id, self._mode, run_id))
            if fills:
                fill_statement = insert(FillRow).values(fills)
                await session.execute(
                    fill_statement.on_conflict_do_update(
                        constraint="order_fill_index",
                        set_={
                            key: getattr(fill_statement.excluded, key)
                            for key in fills[0]
                            if key not in {"order_id", "fill_index"}
                        },
                    )
                )
            return await reconcile_within(
                session,
                self._scope,
                [leg.position_id for leg in legs],
                now=now,
                funding=self._store.funding,
            )

    async def _settle(self, legs: Sequence[LegRecord], results: Sequence[ExecutionResult]) -> None:
        """Give the account back the exposure these fills removed."""
        if self._account is None:
            return
        settlements = [
            ExitSettlement(
                ref=leg.ref,
                entry_side=leg.side,
                entry_price=leg.entry_price,
                closed_quantity=result.filled_quantity,
                exit_notional_usd=result.notional,
                fees_usd=result.fees_usd,
            )
            for leg, result in zip(legs, results, strict=True)
            if result.filled_quantity > 0
        ]
        if settlements:
            await self._account.settle_exit(settlements)
