"""What the risk engine checks about a signal's inputs, independently.

Two jobs, both deliberately clock-driven rather than trusting anything the
strategy already computed:

- **Temporal limits are recomputed, never read off the row.** An opportunity
  records the age its inputs had *when it was detected*; by the time a worker
  evaluates it - and again by the time an order is about to be submitted -
  that age is a lower bound, not the answer. ``check_temporal`` measures from
  the stored timestamps against the current clock, so a signal that sat in a
  queue cannot present a stale book as fresh.
- **Evidence is validated for consistency, not just presence.** An
  opportunity whose evidence describes a different market, a different size,
  a crossed book, or an unsynchronised one is not evidence the engine can
  approve against.

Both return a ``LimitBreach`` describing exactly which limit failed, what was
observed and what was configured, so the caller writes a self-explaining
``risk_events`` row without re-deriving any of it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from itertools import pairwise

from trading_bot.db.models.enums import MarketType, RiskEventType, Side
from trading_bot.strategy.evidence import FillEvidence, LegEvidence
from trading_bot.strategy.models import Leg, Signal

#: A local receipt clock cannot legitimately be ahead of ours by more than the
#: time it takes to hand a message between two coroutines. Anything beyond
#: this is a clock step or a fabricated timestamp, not skew - venue clocks are
#: recorded separately and are not what freshness is judged on.
FUTURE_TOLERANCE = timedelta(milliseconds=250)


@dataclass(frozen=True, slots=True)
class LimitBreach:
    """One failed check, in the vocabulary a ``risk_events`` row needs."""

    event_type: RiskEventType
    reason: str
    limit_name: str
    limit_value: Decimal | None = None
    observed_value: Decimal | None = None


def _age_ms(now: datetime, timestamp: datetime) -> int:
    return int((now - timestamp).total_seconds() * 1000)


def check_temporal(
    signal: Signal,
    now: datetime,
    *,
    max_stale_data_ms: int,
    max_funding_age_ms: int,
    max_latency_ms: int,
) -> LimitBreach | None:
    """Re-measure every age against the current clock. ``None`` if all pass.

    Called at evaluation, again after the approval is durably stored, and
    once more immediately before submission: each of those is a later instant
    than the last, and the whole point of a staleness limit is that passing it
    once does not keep it passed.
    """
    evidence = signal.opportunity.evidence
    if evidence is None:  # structural validation reports this properly
        return None
    limit = Decimal(max_stale_data_ms)
    for name, leg in (("buy", evidence.buy), ("sell", evidence.sell)):
        quote_age = _age_ms(now, leg.quote.local_timestamp)
        if quote_age > max_stale_data_ms:
            return LimitBreach(
                RiskEventType.STALE_DATA,
                f"{name} leg quote is {quote_age}ms old, over the {max_stale_data_ms}ms limit",
                f"{name}_quote_age_ms",
                limit,
                Decimal(quote_age),
            )
        book_age = _age_ms(now, leg.book_local_timestamp)
        if book_age > max_stale_data_ms:
            return LimitBreach(
                RiskEventType.STALE_DATA,
                f"{name} leg book is {book_age}ms old, over the {max_stale_data_ms}ms limit",
                f"{name}_book_age_ms",
                limit,
                Decimal(book_age),
            )

    funding = signal.edge.pricing.funding if signal.edge.pricing is not None else None
    if funding is not None:
        funding_age = _age_ms(now, funding.observed_at)
        if funding_age > max_funding_age_ms:
            return LimitBreach(
                RiskEventType.STALE_DATA,
                f"funding observation is {funding_age}ms old, "
                f"over the {max_funding_age_ms}ms limit",
                "funding_age_ms",
                Decimal(max_funding_age_ms),
                Decimal(funding_age),
            )

    latency = _age_ms(now, signal.generated_at)
    if latency > max_latency_ms:
        return LimitBreach(
            RiskEventType.LATENCY_EXCEEDED,
            f"decision is {latency}ms old, over the {max_latency_ms}ms limit",
            "max_latency_ms",
            Decimal(max_latency_ms),
            Decimal(latency),
        )
    return None


def decision_latency_ms(signal: Signal, now: datetime) -> int:
    return max(0, _age_ms(now, signal.generated_at))


def _incomplete(reason: str, limit_name: str) -> LimitBreach:
    return LimitBreach(RiskEventType.INCOMPLETE_MARKET_DATA, reason, limit_name)


def validate_expiry_timestamp(signal: Signal) -> LimitBreach | None:
    """Reject a timestamp that cannot safely be compared with the UTC clock."""
    if signal.expires_at.tzinfo is None or signal.expires_at.utcoffset() is None:
        return _incomplete("signal expiry timestamp has no timezone", "expires_at")
    return None


def _walk_matches(
    walk: FillEvidence,
    *,
    side: Side,
    quantity: Decimal,
    average_price: Decimal,
    name: str,
) -> LimitBreach | None:
    """Prove a recorded walk really yields the quantity and VWAP it claims."""
    if walk.side is not side:
        return _incomplete(
            f"{name} walk is a {walk.side.value}, not a {side.value}",
            f"{name}_side",
        )
    if walk.requested != quantity or not walk.is_complete or walk.filled != quantity:
        return _incomplete(
            f"{name} walk filled {walk.filled} of {walk.requested}, expected {quantity}",
            f"{name}_fill",
        )
    if walk.average_price != average_price:
        return _incomplete(
            f"{name} evidence averaged {walk.average_price}, expected {average_price}",
            f"{name}_average_price",
        )
    if not walk.levels:
        return _incomplete(f"{name} walk carries no book levels", f"{name}_levels")
    if any(
        not price.is_finite() or not size.is_finite() or price <= 0 or size <= 0
        for price, size in walk.levels
    ):
        return _incomplete(
            f"{name} walk carries a non-positive or non-finite level",
            f"{name}_levels",
        )
    level_quantity = sum((size for _price, size in walk.levels), Decimal(0))
    level_notional = sum((price * size for price, size in walk.levels), Decimal(0))
    if level_quantity != walk.filled or level_notional / level_quantity != walk.average_price:
        return _incomplete(
            f"{name} levels do not reproduce its filled quantity and average price",
            f"{name}_levels",
        )
    prices = [price for price, _size in walk.levels]
    if side is Side.BUY and any(a > b for a, b in pairwise(prices)):
        return _incomplete(f"{name} walk is not ordered from best to worst ask", f"{name}_levels")
    if side is Side.SELL and any(a < b for a, b in pairwise(prices)):
        return _incomplete(f"{name} walk is not ordered from best to worst bid", f"{name}_levels")
    return None


def _leg_matches(leg: Leg, leg_evidence: LegEvidence, name: str) -> LimitBreach | None:
    """The evidence has to describe *this* leg - same market, side and size."""
    if leg_evidence.ref != leg.ref:
        return _incomplete(
            f"{name} leg evidence describes {leg_evidence.ref}, not {leg.ref}",
            f"{name}_market",
        )
    if leg_evidence.side is not leg.side:
        return _incomplete(
            f"{name} leg evidence is a {leg_evidence.side.value}, not a {leg.side.value}",
            f"{name}_side",
        )
    entry = leg_evidence.entry
    if entry.requested != leg.quantity:
        return _incomplete(
            f"{name} leg evidence priced {entry.requested}, not {leg.quantity}",
            f"{name}_quantity",
        )
    if entry.average_price != leg.executable_price:
        return _incomplete(
            f"{name} entry evidence averaged {entry.average_price}, "
            f"not the leg executable price {leg.executable_price}",
            f"{name}_executable_price",
        )
    mismatch = _walk_matches(
        entry,
        side=leg.side,
        quantity=leg.quantity,
        average_price=leg.executable_price,
        name=f"{name}_entry",
    )
    if mismatch is not None:
        return mismatch

    unwind = leg_evidence.unwind
    if leg.unwind_price is None or unwind is None:
        return _incomplete(f"{name} leg has no complete unwind evidence", f"{name}_unwind")
    unwind_side = Side.SELL if leg.side is Side.BUY else Side.BUY
    return _walk_matches(
        unwind,
        side=unwind_side,
        quantity=leg.quantity,
        average_price=leg.unwind_price,
        name=f"{name}_unwind",
    )


def _quote_is_valid(leg_evidence: LegEvidence, name: str) -> LimitBreach | None:
    quote = leg_evidence.quote
    if not quote.bid.is_finite() or not quote.ask.is_finite() or quote.bid <= 0 or quote.ask <= 0:
        return _incomplete(
            f"{name} leg quote has a non-positive or non-finite price ({quote.bid}/{quote.ask})",
            f"{name}_quote_price",
        )
    if (
        not quote.bid_size.is_finite()
        or not quote.ask_size.is_finite()
        or quote.bid_size <= 0
        or quote.ask_size <= 0
    ):
        return _incomplete(
            f"{name} leg quote has a non-positive or non-finite size "
            f"({quote.bid_size}/{quote.ask_size})",
            f"{name}_quote_size",
        )
    if quote.ask < quote.bid:
        # A crossed top of book on one venue means bad data, and the database
        # refuses to store one for the same reason.
        return _incomplete(
            f"{name} leg quote is crossed ({quote.bid} bid > {quote.ask} ask)",
            f"{name}_quote_crossed",
        )
    return None


def _synchronisation_is_valid(leg_evidence: LegEvidence, name: str) -> LimitBreach | None:
    """A book we cannot identify is a book we cannot vouch for.

    Evidence is only ever built from a synchronised local book, which always
    carries the sequence it was synchronised to. Missing or non-positive
    sequences mean the evidence did not come from one.
    """
    if leg_evidence.book_sequence is None or leg_evidence.book_sequence <= 0:
        return _incomplete(
            f"{name} leg book carries no valid sequence ({leg_evidence.book_sequence})",
            f"{name}_book_sequence",
        )
    sequence = leg_evidence.quote.sequence
    if sequence is not None and sequence <= 0:
        return _incomplete(
            f"{name} leg quote carries an invalid sequence ({sequence})",
            f"{name}_quote_sequence",
        )
    return None


def _not_in_the_future(when: datetime, now: datetime, name: str) -> LimitBreach | None:
    if when.tzinfo is None or when.utcoffset() is None:
        return _incomplete(f"{name} timestamp has no timezone", name)
    if when > now + FUTURE_TOLERANCE:
        return _incomplete(
            f"{name} is timestamped {when.isoformat()}, ahead of now ({now.isoformat()})",
            name,
        )
    return None


def validate_evidence(signal: Signal, now: datetime) -> LimitBreach | None:
    """Everything that must be true of a signal's inputs before it can trade."""
    opportunity = signal.opportunity
    evidence = opportunity.evidence
    if evidence is None:
        return _incomplete(
            "opportunity carries no evidence; its inputs cannot be verified", "evidence"
        )

    if signal.edge.opportunity != opportunity:
        return _incomplete(
            "the priced edge describes a different opportunity than the signal",
            "edge_opportunity",
        )
    if signal.expected_net_edge_bps != signal.edge.net_edge_bps:
        return _incomplete(
            f"signal net edge {signal.expected_net_edge_bps} does not match "
            f"the priced edge {signal.edge.net_edge_bps}",
            "expected_net_edge_bps",
        )
    generated = _not_in_the_future(signal.generated_at, now, "signal_generated_at")
    if generated is not None:
        return generated
    evaluated = _not_in_the_future(evidence.evaluated_at, now, "evidence_evaluated_at")
    if evaluated is not None:
        return evaluated

    if evidence.executable_quantity != opportunity.quantity:
        return _incomplete(
            f"evidence priced {evidence.executable_quantity}, "
            f"opportunity is {opportunity.quantity}",
            "executable_quantity",
        )

    legs: tuple[tuple[str, Leg, LegEvidence], ...] = (
        ("buy", opportunity.buy, evidence.buy),
        ("sell", opportunity.sell, evidence.sell),
    )
    for name, leg, leg_evidence in legs:
        for check in (
            _leg_matches(leg, leg_evidence, name),
            _quote_is_valid(leg_evidence, name),
            _synchronisation_is_valid(leg_evidence, name),
            _not_in_the_future(leg_evidence.quote.local_timestamp, now, f"{name}_quote"),
            _not_in_the_future(leg_evidence.book_local_timestamp, now, f"{name}_book"),
        ):
            if check is not None:
                return check

    for name, leg, leg_evidence in legs:
        quote_mid = (leg_evidence.quote.bid + leg_evidence.quote.ask) / Decimal(2)
        if quote_mid != leg.reference_price:
            return _incomplete(
                f"{name} reference price {leg.reference_price} does not match "
                f"the evidence quote midpoint {quote_mid}",
                f"{name}_reference_price",
            )

    if evidence.buy.side is not Side.BUY or evidence.sell.side is not Side.SELL:
        return _incomplete("evidence does not describe one buy and one sell leg", "leg_sides")

    return _validate_funding(signal, now)


def _validate_funding(signal: Signal, now: datetime) -> LimitBreach | None:
    """A perpetual leg without a funding observation was never priceable.

    The cost model refuses to price one (``FUNDING_UNKNOWN``), so a signal
    that reached the risk engine without it was assembled from somewhere
    other than the pricing path and cannot be approved on trust.
    """
    has_perpetual = any(
        leg.ref.market_type is not MarketType.SPOT for leg in signal.opportunity.legs
    )
    if not has_perpetual:
        return None
    funding = signal.edge.pricing.funding if signal.edge.pricing is not None else None
    if funding is None:
        return _incomplete(
            "a perpetual leg is priced without a funding observation", "funding_evidence"
        )
    return _not_in_the_future(funding.observed_at, now, "funding_observation")
