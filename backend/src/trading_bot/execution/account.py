"""Conservative paper balances, margin reservations, and exposure accounting."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING

from trading_bot.core.config import ExecutionConfig, RiskConfig
from trading_bot.db.models.enums import MarketType, Side
from trading_bot.exchange.models import BPS_SCALE, MarketRef
from trading_bot.execution.models import RejectionCode
from trading_bot.strategy.models import Signal

if TYPE_CHECKING:
    from trading_bot.execution.coordinator import ExecutionAttempt


@dataclass(frozen=True, slots=True)
class AccountRejection:
    code: RejectionCode
    detail: str


@dataclass(frozen=True, slots=True)
class AccountReservation:
    intent_id: str
    cash_usd: Decimal
    gross_usd: Decimal
    borrow_usd: Decimal = Decimal(0)
    spot_sells: tuple[tuple[str, Decimal], ...] = ()
    positions: tuple[tuple[tuple[str, MarketType], Decimal], ...] = ()


@dataclass(frozen=True, slots=True)
class PaperPositionSeed:
    symbol: str
    market_type: MarketType
    side: Side
    quantity: Decimal
    notional_usd: Decimal
    fees_usd: Decimal


class PaperAccount:
    """A minimal account model shared by concurrent execution workers.

    It is intentionally conservative: proceeds from a simultaneous sale do
    not fund the other leg, perpetual notional reserves margin, and spot sells
    require inventory unless an explicit borrow facility and cap are present.
    """

    def __init__(
        self,
        execution: ExecutionConfig,
        risk: RiskConfig,
        *,
        pays_fees_in_bnb: bool = False,
        max_fee_bps: Decimal = Decimal(0),
    ) -> None:
        self._cash = Decimal(str(execution.paper_cash_usd))
        self._inventory = {
            symbol.upper(): Decimal(str(quantity))
            for symbol, quantity in execution.paper_spot_inventory.items()
        }
        self._pays_fees_in_bnb = pays_fees_in_bnb
        self._max_fee_bps = max_fee_bps
        self._bnb = Decimal(str(execution.paper_bnb_balance))
        self._bnb_price = (
            Decimal(str(execution.paper_bnb_price_usd))
            if execution.paper_bnb_price_usd is not None
            else None
        )
        self._allow_borrow = execution.paper_allow_margin_borrow
        self._max_borrow = Decimal(str(execution.paper_max_borrow_usd))
        self._leverage = Decimal(str(execution.paper_perp_leverage))
        self._max_order = Decimal(str(risk.max_order_notional_usd))
        self._max_position = Decimal(str(risk.max_position_notional_usd))
        self._max_gross = Decimal(str(risk.max_total_exposure_usd))
        self._gross = Decimal(0)
        self._net = Decimal(0)
        self._borrowed = Decimal(0)
        self._reserved_borrow = Decimal(0)
        self._reserved_spot_sells: dict[str, Decimal] = {}
        self._perp_margin = Decimal(0)
        self._reserved_cash = Decimal(0)
        self._reserved_gross = Decimal(0)
        self._position_gross: dict[tuple[str, MarketType], Decimal] = {}
        self._reserved_position_gross: dict[tuple[str, MarketType], Decimal] = {}
        self._reservations: dict[str, AccountReservation] = {}
        self._completed_intents: OrderedDict[str, None] = OrderedDict()
        # The adapter cache stores two client order ids per basis attempt.
        # Forgetting account idempotency before those orders is safe; doing it
        # after them could re-simulate fills without counting their exposure.
        self._max_completed_intents = max(1, execution.max_cached_orders // 2)
        self._lock = asyncio.Lock()

    async def reserve(
        self, signal: Signal, intent_id: str
    ) -> AccountReservation | AccountRejection:
        async with self._lock:
            if intent_id in self._completed_intents:
                self._completed_intents.move_to_end(intent_id)
                return AccountReservation(intent_id, Decimal(0), Decimal(0))
            existing = self._reservations.get(intent_id)
            if existing is not None:
                return existing

            gross = sum((leg.notional for leg in signal.legs), Decimal(0))
            for leg in signal.legs:
                if leg.notional > self._max_order:
                    return AccountRejection(
                        RejectionCode.EXPOSURE_LIMIT,
                        f"leg notional {leg.notional} exceeds {self._max_order}",
                    )
                position_key = _position_key(leg.ref)
                current_position = self._position_gross.get(position_key, Decimal(0))
                reserved_position = self._reserved_position_gross.get(position_key, Decimal(0))
                if current_position + reserved_position + leg.notional > self._max_position:
                    return AccountRejection(
                        RejectionCode.EXPOSURE_LIMIT,
                        f"{leg.ref} exposure would exceed {self._max_position}",
                    )
            if self._gross + self._reserved_gross + gross > self._max_gross:
                return AccountRejection(
                    RejectionCode.EXPOSURE_LIMIT,
                    f"gross exposure would exceed {self._max_gross}",
                )

            cash_needed = Decimal(0)
            borrow_needed = Decimal(0)
            spot_sells: list[tuple[str, Decimal]] = []
            for leg in signal.legs:
                if leg.ref.market_type is MarketType.SPOT:
                    if leg.side is Side.BUY:
                        cash_needed += leg.notional
                    else:
                        held = self._inventory.get(
                            leg.ref.symbol, Decimal(0)
                        ) - self._reserved_spot_sells.get(leg.ref.symbol, Decimal(0))
                        missing = max(Decimal(0), leg.quantity - held)
                        borrow_needed += missing * leg.executable_price
                        spot_sells.append((leg.ref.symbol, leg.quantity))
                else:
                    cash_needed += leg.notional / self._leverage
            if not self._pays_fees_in_bnb:
                cash_needed += gross * self._max_fee_bps / BPS_SCALE
            if borrow_needed:
                if not self._allow_borrow:
                    return AccountRejection(
                        RejectionCode.BORROW_UNAVAILABLE,
                        f"spot sell needs {borrow_needed} USD of borrowed inventory",
                    )
                if self._borrowed + self._reserved_borrow + borrow_needed > self._max_borrow:
                    return AccountRejection(
                        RejectionCode.BORROW_UNAVAILABLE,
                        f"borrow would exceed {self._max_borrow} USD",
                    )
            available = self._cash - self._perp_margin - self._reserved_cash
            if cash_needed > available:
                return AccountRejection(
                    RejectionCode.INSUFFICIENT_MARGIN,
                    f"needs {cash_needed} USD, only {available} USD available",
                )

            reservation = AccountReservation(
                intent_id,
                cash_needed,
                gross,
                borrow_needed,
                tuple(spot_sells),
                tuple((_position_key(leg.ref), leg.notional) for leg in signal.legs),
            )
            self._reservations[intent_id] = reservation
            self._reserved_cash += cash_needed
            self._reserved_gross += gross
            self._reserved_borrow += borrow_needed
            for ref, notional in reservation.positions:
                self._reserved_position_gross[ref] = (
                    self._reserved_position_gross.get(ref, Decimal(0)) + notional
                )
            for symbol, quantity in spot_sells:
                self._reserved_spot_sells[symbol] = (
                    self._reserved_spot_sells.get(symbol, Decimal(0)) + quantity
                )
            return reservation

    def restore(self, positions: list[PaperPositionSeed]) -> None:
        """Rebuild balances/exposure from durable open positions at startup."""
        if self._reservations:
            raise RuntimeError("cannot restore an account after execution has started")
        for position in positions:
            signed = position.notional_usd if position.side is Side.BUY else -position.notional_usd
            self._gross += position.notional_usd
            position_key = (position.symbol, position.market_type)
            self._position_gross[position_key] = (
                self._position_gross.get(position_key, Decimal(0)) + position.notional_usd
            )
            self._net += signed
            self._pay_fee(position.fees_usd)
            if position.market_type is MarketType.SPOT:
                quantity = position.quantity if position.side is Side.BUY else -position.quantity
                self._inventory[position.symbol] = (
                    self._inventory.get(position.symbol, Decimal(0)) + quantity
                )
                self._cash -= signed
                if self._inventory[position.symbol] < 0:
                    self._borrowed += abs(signed)
            else:
                self._perp_margin += position.notional_usd / self._leverage

    async def settle(self, reservation: AccountReservation, attempt: ExecutionAttempt) -> None:
        async with self._lock:
            if not self._release_locked(reservation):
                return
            for outcome in attempt.legs:
                result = outcome.result
                if result.filled_quantity <= 0 or result.average_price is None:
                    continue
                notional = result.notional
                signed = notional if outcome.leg.side is Side.BUY else -notional
                self._gross += notional
                position_key = _position_key(outcome.leg.ref)
                self._position_gross[position_key] = (
                    self._position_gross.get(position_key, Decimal(0)) + notional
                )
                self._net += signed
                self._pay_fee(result.fees_usd)
                if outcome.leg.ref.market_type is MarketType.SPOT:
                    inventory = self._inventory.get(outcome.leg.ref.symbol, Decimal(0))
                    if outcome.leg.side is Side.BUY:
                        self._cash -= notional
                        inventory += result.filled_quantity
                    else:
                        self._cash += notional
                        if result.filled_quantity > inventory:
                            borrowed = (result.filled_quantity - inventory) * result.average_price
                            self._borrowed += borrowed
                        inventory -= result.filled_quantity
                    self._inventory[outcome.leg.ref.symbol] = inventory
                else:
                    self._perp_margin += notional / self._leverage
            self._remember_completed(reservation.intent_id)

    async def release(self, reservation: AccountReservation) -> None:
        """Release a hypothetical probe's checks without mutating the portfolio."""
        async with self._lock:
            if self._release_locked(reservation):
                self._remember_completed(reservation.intent_id)

    def _release_locked(self, reservation: AccountReservation) -> bool:
        if self._reservations.pop(reservation.intent_id, None) is None:
            return False
        self._reserved_cash -= reservation.cash_usd
        self._reserved_gross -= reservation.gross_usd
        self._reserved_borrow -= reservation.borrow_usd
        for ref, notional in reservation.positions:
            left = self._reserved_position_gross.get(ref, Decimal(0)) - notional
            if left > 0:
                self._reserved_position_gross[ref] = left
            else:
                self._reserved_position_gross.pop(ref, None)
        for symbol, quantity in reservation.spot_sells:
            left = self._reserved_spot_sells.get(symbol, Decimal(0)) - quantity
            if left > 0:
                self._reserved_spot_sells[symbol] = left
            else:
                self._reserved_spot_sells.pop(symbol, None)
        return True

    def _remember_completed(self, intent_id: str) -> None:
        self._completed_intents[intent_id] = None
        self._completed_intents.move_to_end(intent_id)
        while len(self._completed_intents) > self._max_completed_intents:
            self._completed_intents.popitem(last=False)

    @property
    def gross_exposure_usd(self) -> Decimal:
        return self._gross

    @property
    def net_exposure_usd(self) -> Decimal:
        return self._net

    @property
    def cash_usd(self) -> Decimal:
        return self._cash

    def _pay_fee(self, fee_usd: Decimal) -> None:
        if self._pays_fees_in_bnb:
            if self._bnb_price is None:  # guarded by Settings validation
                raise RuntimeError("BNB fee payment needs a BNB/USD price")
            quantity = fee_usd / self._bnb_price
            if quantity > self._bnb:
                raise RuntimeError("paper BNB balance exhausted")
            self._bnb -= quantity
        else:
            self._cash -= fee_usd


def _position_key(ref: MarketRef) -> tuple[str, MarketType]:
    return (ref.symbol, ref.market_type)
