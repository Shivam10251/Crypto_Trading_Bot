"""The strategy layer's vocabulary: opportunities, costs, edges and signals.

These are pure domain types. Nothing here imports the database, the exchange
client, FastAPI or the dashboard - which is what lets one strategy object run
unchanged in backtest, paper and live modes.

The split between ``Opportunity`` (a discrepancy that exists) and ``Signal``
(an intent to act on it) is deliberate: an opportunity can be detected and
never traded, and the gap between the two is exactly what research measures.
Costs are itemised rather than netted into one number so that a rejected
opportunity can still answer "what ate the edge?".
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from trading_bot.db.models.enums import Side
from trading_bot.exchange.models import BPS_SCALE, MarketRef
from trading_bot.strategy.evidence import MarketEvidence, PricingEvidence


def to_bps(value: Decimal, reference: Decimal) -> Decimal:
    """``value`` as basis points of ``reference``; 0 when there is no reference."""
    if reference <= 0:
        return Decimal(0)
    return value / reference * BPS_SCALE


@dataclass(frozen=True, slots=True)
class Leg:
    """One side of a trade, priced at what the book would actually give us.

    ``reference_price`` is the mid at detection and ``executable_price`` is the
    average price of walking the book for ``quantity``. The difference between
    them is slippage - measured, never assumed.
    """

    ref: MarketRef
    side: Side
    reference_price: Decimal
    executable_price: Decimal
    quantity: Decimal
    # What unwinding this leg would fetch against the book we can see now: a
    # leg bought is sold back into the bids, a leg sold is bought back from the
    # asks. ``None`` when that book could not fill the whole quantity - an
    # unpriceable unwind, which the cost model refuses rather than substituting
    # a number for. It is a *modelled* exit, never an entry price.
    unwind_price: Decimal | None = None

    def __post_init__(self) -> None:
        if self.quantity <= 0:
            raise ValueError(f"leg quantity must be positive for {self.ref}")
        if self.reference_price <= 0 or self.executable_price <= 0:
            raise ValueError(f"leg prices must be positive for {self.ref}")
        if self.unwind_price is not None and self.unwind_price <= 0:
            raise ValueError(f"leg unwind price must be positive for {self.ref}")

    @property
    def notional(self) -> Decimal:
        return self.executable_price * self.quantity

    @property
    def slippage(self) -> Decimal:
        """Per-unit cost of crossing to an executable price; never negative.

        A buy that fills above the mid and a sell that fills below it both cost
        us, so the sign is normalised by side.
        """
        signed = (
            self.executable_price - self.reference_price
            if self.side is Side.BUY
            else self.reference_price - self.executable_price
        )
        return max(signed, Decimal(0))

    @property
    def slippage_usd(self) -> Decimal:
        return self.slippage * self.quantity

    @property
    def slippage_bps(self) -> Decimal:
        return to_bps(self.slippage, self.reference_price)

    @property
    def unwind_slippage(self) -> Decimal | None:
        """Per-unit cost of unwinding, measured rather than assumed.

        The unwind crosses the spread the other way - a bought leg is sold
        into the bids - so it is a different walk of the same book, not a copy
        of the entry. ``None`` when the book could not fill it.
        """
        if self.unwind_price is None:
            return None
        # Unwinding reverses the side: a BUY leg exits by selling.
        signed = (
            self.reference_price - self.unwind_price
            if self.side is Side.BUY
            else self.unwind_price - self.reference_price
        )
        return max(signed, Decimal(0))

    @property
    def unwind_slippage_usd(self) -> Decimal | None:
        unwind_slippage = self.unwind_slippage
        return None if unwind_slippage is None else unwind_slippage * self.quantity

    @property
    def unwind_notional(self) -> Decimal | None:
        return None if self.unwind_price is None else self.unwind_price * self.quantity


@dataclass(frozen=True, slots=True)
class Opportunity:
    """A priced discrepancy between two markets, before costs are applied.

    ``gross_edge`` is measured mid-to-mid: the cost of crossing to executable
    prices belongs in ``CostBreakdown.slippage_usd`` so that research can
    attribute lost edge to spreads rather than having it silently netted away.

    It is a **theoretical convergence edge**, not profit: it is what the trade
    is worth if the two mids meet, which is an assumption about the future and
    is recorded as one (``CostsConfig.assumed_terminal_basis_bps``). Realised
    price P&L on a basis position is ``signed_quantity x (entry basis - exit
    basis)``, and only Phase 8's fills can say what the exit basis was.
    """

    strategy: str
    detected_at: datetime
    buy: Leg
    sell: Leg
    # The price everything is expressed against - the spot leg's mid.
    reference_price: Decimal
    # The executable quantity: rounded down to an increment valid on BOTH legs.
    quantity: Decimal
    notional_usd: Decimal
    gross_edge_bps: Decimal
    gross_edge_usd: Decimal
    # What the strategy asked for before venue lot filters cut it down. Kept so
    # a stored row can show how much of the intended size actually survived.
    requested_notional_usd: Decimal | None = None
    # Size the thinner leg could actually absorb near the mid.
    liquidity_usd: Decimal | None = None
    # Age of the oldest data behind this decision.
    latency_ms: int | None = None
    # How long this discrepancy has persisted in the same direction. Shorter
    # than round-trip latency means it was never executable by this system.
    duration_ms: int | None = None
    # Everything needed to re-derive this decision once retention has deleted
    # the raw feed it came from. None only for opportunities built by tests.
    evidence: MarketEvidence | None = None

    @property
    def direction(self) -> Side:
        """Side of the spot leg - how the database records a two-legged trade."""
        return Side.BUY if self.buy.ref.market_type.value == "SPOT" else Side.SELL

    @property
    def legs(self) -> tuple[Leg, Leg]:
        return (self.buy, self.sell)


@dataclass(frozen=True, slots=True)
class CostBreakdown:
    """Everything standing between a gross spread and money kept.

    Every field is a positive cost except ``funding_usd``, which is signed: a
    short perpetual leg *receives* funding when the rate is positive, and
    pretending that is always a cost would understate the edge as dishonestly
    as ignoring it would overstate it.
    """

    fees_usd: Decimal
    slippage_usd: Decimal
    funding_usd: Decimal
    buffer_usd: Decimal
    other_usd: Decimal = Decimal(0)
    borrow_usd: Decimal = Decimal(0)

    @property
    def total_usd(self) -> Decimal:
        return (
            self.fees_usd
            + self.slippage_usd
            + self.funding_usd
            + self.buffer_usd
            + self.other_usd
            + self.borrow_usd
        )


@dataclass(frozen=True, slots=True)
class Edge:
    """Gross edge, the costs against it, and what survives.

    ``net_edge`` is what the *theoretical convergence* edge is worth after
    costs, under the assumptions in ``pricing``. It is not realised profit and
    nothing here claims it is.
    """

    opportunity: Opportunity
    costs: CostBreakdown
    net_edge_usd: Decimal
    net_edge_bps: Decimal
    # What the cost model assumed about the perpetual leg's holding period.
    funding_horizon: timedelta | None = None
    # Rates, roles, funding observation and the assumption snapshot behind the
    # numbers above - stored, so a row explains itself years later.
    pricing: PricingEvidence | None = None

    @property
    def gross_edge_usd(self) -> Decimal:
        return self.opportunity.gross_edge_usd

    @property
    def gross_edge_bps(self) -> Decimal:
        return self.opportunity.gross_edge_bps

    @property
    def is_profitable(self) -> bool:
        return self.net_edge_usd > 0


@dataclass(frozen=True, slots=True)
class Signal:
    """A strategy's intent to trade a validated opportunity."""

    strategy: str
    generated_at: datetime
    opportunity: Opportunity
    edge: Edge
    expected_net_edge_bps: Decimal
    expires_at: datetime

    @property
    def legs(self) -> tuple[Leg, Leg]:
        return self.opportunity.legs

    def is_expired(self, now: datetime) -> bool:
        return now >= self.expires_at


class RejectionReason(StrEnum):
    """Why an opportunity was not traded. Stored, never discarded.

    "How many opportunities existed, and what killed each one?" is the central
    research question; an unexplained rejection cannot answer it.
    """

    NOT_LIVE = "NOT_LIVE"
    # Kept for the market-wide case; the three below say which input was old,
    # because a fresh message of one kind does not make another kind fresh.
    STALE_DATA = "STALE_DATA"
    STALE_QUOTE = "STALE_QUOTE"
    STALE_BOOK = "STALE_BOOK"
    STALE_FUNDING = "STALE_FUNDING"
    BOOK_NOT_SYNCED = "BOOK_NOT_SYNCED"
    LATENCY_EXCEEDED = "LATENCY_EXCEEDED"
    # A negative feed latency: exchange time after local receipt. The latency
    # is then unmeasured, never "fast enough".
    CLOCK_SKEW = "CLOCK_SKEW"
    INSUFFICIENT_LIQUIDITY = "INSUFFICIENT_LIQUIDITY"
    BELOW_MIN_NOTIONAL = "BELOW_MIN_NOTIONAL"
    # Rounding to a valid lot left less than the venue's minimum order size.
    BELOW_MIN_QUANTITY = "BELOW_MIN_QUANTITY"
    # The book could not fill the modelled unwind, so the exit has no price.
    # Doubling the entry instead would be a guess, not a conservative estimate.
    UNWIND_NOT_FILLABLE = "UNWIND_NOT_FILLABLE"
    # The trade needs spot sold short, which needs inventory or a margin borrow.
    SPOT_SHORT_UNAVAILABLE = "SPOT_SHORT_UNAVAILABLE"
    BORROW_COST_UNKNOWN = "BORROW_COST_UNKNOWN"
    FUNDING_UNKNOWN = "FUNDING_UNKNOWN"
    BELOW_MIN_EDGE = "BELOW_MIN_EDGE"
    SIGNAL_EXPIRED = "SIGNAL_EXPIRED"


@dataclass(frozen=True, slots=True)
class PricingRefusal:
    """The cost model declining to price, and saying which cost it could not.

    Returned instead of an ``Edge`` so the reason survives into the research
    record. A refusal is not a zero edge: an opportunity nobody could price is
    a different fact from one priced at nothing, and conflating them would
    corrupt every query asking what survived costs.
    """

    reason: RejectionReason
    detail: str


#: What ``CostModel.estimate`` returns: a priced edge, or why it refused.
PricingResult = Edge | PricingRefusal


@dataclass
class DetectionStats:
    """Why the markets a strategy saw did not all produce an opportunity.

    The counts matter as much as the opportunities: "nothing to trade" and
    "half the feed was stale" look identical in a list of zero opportunities.
    """

    pairs_seen: int = 0
    pairs_usable: int = 0
    unusable: Counter[RejectionReason] = field(default_factory=Counter)
    # Both legs usable, but the prices agree: nothing to trade, not a rejection.
    no_basis: int = 0


@dataclass(frozen=True, slots=True)
class ValidationResult:
    """Whether a signal may proceed, and if not, exactly why."""

    is_valid: bool
    reason: RejectionReason | None = None
    detail: str | None = None

    @classmethod
    def valid(cls) -> ValidationResult:
        return cls(is_valid=True)

    @classmethod
    def rejected(cls, reason: RejectionReason, detail: str) -> ValidationResult:
        return cls(is_valid=False, reason=reason, detail=detail)
