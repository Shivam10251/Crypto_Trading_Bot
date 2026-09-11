"""Keeping perpetual funding rates current.

Funding is a real cost of holding a perpetual leg, but it arrives over REST
rather than on the stream, and it changes on the venue's schedule rather than
per tick. So it is polled on a slow cadence and handed to the strategy layer,
which never talks to an adapter itself.

One bulk request covers every perpetual on the venue at weight 10, so the cost
is the same whether one pair is monitored or a hundred.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable

from trading_bot.core.logging import get_logger
from trading_bot.db.models.enums import MarketType
from trading_bot.exchange.base import ExchangeAdapter
from trading_bot.exchange.errors import ExchangeError
from trading_bot.exchange.models import FundingInfo, MarketRef

logger = get_logger(__name__)


class FundingTracker:
    """Polls bulk funding rates and holds the latest state per market."""

    def __init__(
        self,
        adapter: ExchangeAdapter,
        refs: Iterable[MarketRef],
        *,
        interval_seconds: float = 60.0,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        self._adapter = adapter
        # Only the perpetuals actually monitored; spot legs pay no funding.
        self._wanted = {ref for ref in refs if ref.market_type is not MarketType.SPOT}
        self._interval = interval_seconds
        self._rates: dict[MarketRef, FundingInfo] = {}
        self._without_interval: set[str] = set()

    @property
    def rates(self) -> dict[MarketRef, FundingInfo]:
        return dict(self._rates)

    @property
    def markets_without_interval(self) -> set[str]:
        """Monitored perpetuals whose settlement interval the venue omits.

        Their funding cost cannot be estimated, so the cost model refuses them
        rather than assuming a period. Surfaced so the count is visible rather
        than silently shrinking the tradeable universe.
        """
        return set(self._without_interval)

    async def refresh(self) -> int:
        """Fetch once. Returns how many monitored markets were updated."""
        if not self._wanted:
            return 0
        rates = await self._adapter.get_funding_rates(MarketType.PERPETUAL)
        updated = {rate.ref: rate for rate in rates if rate.ref in self._wanted}
        self._rates = updated
        self._without_interval = {
            ref.symbol for ref, rate in updated.items() if rate.funding_interval_hours is None
        }
        return len(updated)

    async def run(self) -> None:
        """Refresh on a slow cadence; a failed poll keeps the last known rates.

        Stale funding is better than none: it is a slow-moving cost, and
        dropping it would make every opportunity unpriceable on one bad request.
        """
        while True:
            try:
                count = await self.refresh()
            except ExchangeError as exc:
                logger.warning("funding.refresh_failed", error=str(exc), kept=len(self._rates))
            else:
                logger.debug(
                    "funding.refreshed", markets=count, without_interval=len(self._without_interval)
                )
            await asyncio.sleep(self._interval)
