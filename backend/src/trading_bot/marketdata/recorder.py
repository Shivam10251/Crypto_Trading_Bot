"""Persists what the engine sees: sampled quotes and infrastructure events.

Not every quote is stored - BTCUSDT alone changes top of book dozens of times a
second per venue. Each market is sampled once per interval and a row is written
only when the quote actually changed, so the table holds observations, never a
duplicated or interpolated value.

System events (connects, disconnects, gaps, stale data) are written as they
happen: they are what explains a hole in the research data later.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable, Sequence
from contextlib import AbstractAsyncContextManager
from typing import Any

from sqlalchemy import func, insert
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from trading_bot.core.logging import get_logger
from trading_bot.db.models import Market, MarketData, SystemEvent
from trading_bot.exchange.models import MarketRef, MarketSpec, Quote
from trading_bot.marketdata.models import MarketDataEvent, MarketSnapshot

logger = get_logger(__name__)

COMPONENT = "market_data"
# Events held for retry while the database is unreachable. Beyond this the
# oldest are dropped - with a warning - rather than growing without bound.
MAX_PENDING_EVENTS = 1000

SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]

_IDENTITY = ("venue", "symbol", "market_type")
# Venue fees are only known with credentials; a start without them must not
# erase fees someone recorded.
_KEEP_WHEN_NULL = ("maker_fee_bps", "taker_fee_bps")


async def register_markets(
    session: AsyncSession, specs: Sequence[MarketSpec]
) -> dict[MarketRef, int]:
    """Upsert the monitored instruments and return their row ids.

    Reference data is refreshed from the venue on every start, so ``markets``
    never drifts from what the exchange currently reports.
    """
    if not specs:
        return {}
    rows = [
        {
            "venue": spec.ref.venue,
            "symbol": spec.ref.symbol,
            "market_type": spec.ref.market_type,
            "base_asset": spec.base_asset,
            "quote_asset": spec.quote_asset,
            "tick_size": spec.tick_size,
            "step_size": spec.step_size,
            "min_notional": spec.min_notional,
            "maker_fee_bps": spec.maker_fee_bps,
            "taker_fee_bps": spec.taker_fee_bps,
            "contract_size": spec.contract_size,
            "settlement_asset": spec.settlement_asset,
            "is_active": spec.is_active,
        }
        for spec in specs
    ]
    statement = pg_insert(Market).values(rows)
    updates: dict[str, Any] = {
        key: statement.excluded[key] for key in rows[0] if key not in _IDENTITY
    }
    for key in _KEEP_WHEN_NULL:
        updates[key] = func.coalesce(statement.excluded[key], getattr(Market, key))
    # ON CONFLICT updates bypass the ORM's onupdate hook.
    updates["updated_at"] = func.now()
    statement = statement.on_conflict_do_update(index_elements=_IDENTITY, set_=updates)
    result = await session.execute(
        statement.returning(Market.id, Market.venue, Market.symbol, Market.market_type)
    )
    return {
        MarketRef(venue=row.venue, symbol=row.symbol, market_type=row.market_type): row.id
        for row in result
    }


def quote_row(market_id: int, snapshot: MarketSnapshot, quote: Quote) -> dict[str, Any]:
    return {
        "market_id": market_id,
        "bid": quote.bid,
        "ask": quote.ask,
        "bid_size": quote.bid_size,
        "ask_size": quote.ask_size,
        "mid_price": quote.mid_price,
        "spread": quote.spread,
        "spread_bps": quote.spread_bps,
        "volume_24h": snapshot.volume_24h,
        # The quote's own clock only: NULL for Binance spot, never local time.
        "exchange_timestamp": quote.exchange_timestamp,
        "local_timestamp": quote.local_timestamp,
        "latency_ms": quote.latency_ms,
        "sequence": quote.sequence,
    }


def event_row(event: MarketDataEvent) -> dict[str, Any]:
    return {
        "occurred_at": event.occurred_at,
        "event_type": event.event_type,
        "severity": event.severity,
        "component": COMPONENT,
        "message": event.message,
        "context": event.context or None,
    }


async def write_batch(
    session: AsyncSession,
    quote_rows: Sequence[dict[str, Any]],
    event_rows: Sequence[dict[str, Any]],
) -> None:
    if quote_rows:
        await session.execute(insert(MarketData), list(quote_rows))
    if event_rows:
        await session.execute(insert(SystemEvent), list(event_rows))


class MarketDataRecorder:
    """Samples the engine into the database on a fixed interval."""

    def __init__(
        self,
        market_ids: dict[MarketRef, int],
        session_factory: SessionFactory,
        *,
        interval_seconds: float,
    ) -> None:
        self._market_ids = market_ids
        self._session_factory = session_factory
        self._interval = interval_seconds
        self._last_written: dict[MarketRef, Quote] = {}
        self._pending: deque[MarketDataEvent] = deque(maxlen=MAX_PENDING_EVENTS)
        self.rows_written = 0
        self.failures = 0

    def record_event(self, event: MarketDataEvent) -> None:
        """Engine listener; queued and written with the next batch."""
        if len(self._pending) == MAX_PENDING_EVENTS:
            logger.warning("recorder.event_dropped", dropped=self._pending[0].message)
        self._pending.append(event)

    async def flush(self, snapshots: Sequence[MarketSnapshot]) -> int:
        """Write changed quotes and pending events; returns quote rows written.

        A database failure is logged and retried next interval - it must never
        stop the feed that the rest of the platform depends on.
        """
        changed: list[tuple[MarketRef, int, MarketSnapshot, Quote]] = []
        for snapshot in snapshots:
            quote = snapshot.quote
            market_id = self._market_ids.get(snapshot.ref)
            # Identity, not equality: the engine replaces the quote object on
            # every update, so "same object" means "nothing new arrived".
            if quote is None or market_id is None or self._last_written.get(snapshot.ref) is quote:
                continue
            changed.append((snapshot.ref, market_id, snapshot, quote))
        events = list(self._pending)
        if not changed and not events:
            return 0

        try:
            async with self._session_factory() as session:
                await write_batch(
                    session,
                    [quote_row(market_id, snap, quote) for _, market_id, snap, quote in changed],
                    [event_row(event) for event in events],
                )
        except Exception as exc:
            self.failures += 1
            logger.warning("recorder.write_failed", error=str(exc), pending_events=len(events))
            return 0

        written = {id(event) for event in events}
        self._pending = deque(
            (event for event in self._pending if id(event) not in written),
            maxlen=MAX_PENDING_EVENTS,
        )
        for ref, _, _, quote in changed:
            self._last_written[ref] = quote
        self.rows_written += len(changed)
        return len(changed)

    async def run(self, snapshots: Callable[[], Sequence[MarketSnapshot]]) -> None:
        while True:
            await asyncio.sleep(self._interval)
            await self.flush(snapshots())
