"""What a replay is made of: recorded observations, each with the instant it became knowable.

**Ordered by local receipt time, never by the exchange clock.** An event's
``available_at`` is when *this system* received it (``local_timestamp``). The
exchange timestamp is earlier by the feed latency, and ordering by it would
hand the replay a quote before the process that recorded it could have seen
it - look-ahead by exactly the latency the live strategy suffers.

**Total, deterministic order.** ``(available_at, kind, source_id)``. Equal
receipt times are genuinely simultaneous - all of them are visible at that
instant - so the tie-break only fixes the order they are applied in, and the
row id makes that order a property of the dataset rather than of a query plan.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import IntEnum

from trading_bot.exchange.models import FundingInfo, MarketRef, OrderBook, Quote


class EventKind(IntEnum):
    """Also the tie-break rank between kinds at one receipt instant."""

    FUNDING = 0
    BOOK = 1
    QUOTE = 2


EventPayload = Quote | OrderBook | FundingInfo


@dataclass(frozen=True, slots=True)
class ReplayEvent:
    kind: EventKind
    ref: MarketRef
    #: Local receipt time: the earliest instant any component may observe it.
    available_at: datetime
    #: The recorded row's id in its own table; makes the order total.
    source_id: int
    payload: EventPayload
    #: Venue sequence (quote update id, book ``lastUpdateId``); ``None`` for
    #: funding, which has none.
    sequence: int | None = None
    #: Rolling 24h base volume sampled with a quote, when recorded.
    volume_24h: Decimal | None = None

    @property
    def order_key(self) -> tuple[datetime, int, int]:
        return (self.available_at, int(self.kind), self.source_id)

    def digest_into(self, digest: hashlib._Hash, *, role: str = "event") -> None:
        """Feed every field any component can observe to a dataset fingerprint.

        Identity (kind, market, receipt time, row id, venue sequence) and the
        whole payload: both clocks, prices, sizes, volume, every depth level
        and the completeness flags, and every funding field. A change to
        anything the strategy, the simulator or valuation could read changes
        the fingerprint. Each field is length-delimited by a separator and
        each record by a terminator, so values cannot run into each other.
        ``role`` separates initialization state from in-window events.
        """
        payload = self.payload
        parts: list[object] = [
            role,
            self.kind.name,
            self.ref.venue,
            self.ref.symbol,
            self.ref.market_type.value,
            self.available_at.isoformat(),
            self.source_id,
            self.sequence,
        ]
        if isinstance(payload, Quote):
            parts += [
                payload.local_timestamp.isoformat(),
                _iso(payload.exchange_timestamp),
                payload.sequence,
                payload.bid,
                payload.ask,
                payload.bid_size,
                payload.ask_size,
                self.volume_24h,
            ]
        elif isinstance(payload, OrderBook):
            parts += [
                payload.local_timestamp.isoformat(),
                _iso(payload.exchange_timestamp),
                payload.sequence,
                payload.bids_complete,
                payload.asks_complete,
                len(payload.bids),
                len(payload.asks),
                ",".join(f"{level.price}:{level.size}" for level in payload.bids),
                ",".join(f"{level.price}:{level.size}" for level in payload.asks),
            ]
        else:
            parts += [
                payload.local_timestamp.isoformat(),
                payload.last_funding_rate,
                payload.mark_price,
                payload.index_price,
                payload.next_funding_time.isoformat(),
                payload.funding_interval_hours,
            ]
        digest.update(("\x1f".join(str(part) for part in parts) + "\x1e").encode())


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None
