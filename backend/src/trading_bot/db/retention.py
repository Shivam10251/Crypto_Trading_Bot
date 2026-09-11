"""Retention policies for high-volume raw data.

Rationale: top-of-book updates for 50+ markets arrive several times per second
per market. Keeping every row forever buys little research value - the decisions
made from that data are already preserved in ``opportunities`` - while making
every query and backup slower.

What is never purged: opportunities, signals, orders, fills, positions,
portfolio and P&L snapshots, risk events and system events. Those are the
research dataset and the audit trail.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute

from trading_bot.core.config import RetentionConfig
from trading_bot.core.logging import get_logger
from trading_bot.db.models.market import MarketData, MarketTrade, OrderBookSnapshot

logger = get_logger(__name__)


@dataclass(frozen=True)
class RetentionPolicy:
    """One purge rule: rows in ``table`` older than ``max_age_days`` go."""

    name: str
    max_age_days: int

    def cutoff(self, now: datetime) -> datetime:
        return now - timedelta(days=self.max_age_days)


def policies_from_config(config: RetentionConfig) -> tuple[RetentionPolicy, ...]:
    """Build the policy set from configuration. Order is stable for testing."""
    return (
        RetentionPolicy("market_data", config.market_data_days),
        RetentionPolicy("order_books", config.order_books_days),
        RetentionPolicy("trades_market", config.trades_market_days),
    )


# Only these three tables are ever purged. The union type keeps the mapping
# honest: adding a table here requires adding it to the type as well.
PurgeModel = type[MarketData] | type[OrderBookSnapshot] | type[MarketTrade]

# Purges filter on local_timestamp - when we received the data - rather than the
# exchange clock, which a bad feed can misreport.
_PURGE_TARGETS: dict[str, tuple[PurgeModel, InstrumentedAttribute[datetime]]] = {
    "market_data": (MarketData, MarketData.local_timestamp),
    "order_books": (OrderBookSnapshot, OrderBookSnapshot.local_timestamp),
    "trades_market": (MarketTrade, MarketTrade.local_timestamp),
}


async def purge_expired(
    session: AsyncSession,
    config: RetentionConfig,
    *,
    now: datetime | None = None,
    policies: Sequence[RetentionPolicy] | None = None,
) -> dict[str, int]:
    """Delete rows older than their policy allows.

    Deletes in batches so a long purge never holds a table-wide lock. Returns
    the number of rows removed per table; an empty mapping when retention is
    disabled.
    """
    if not config.enabled:
        logger.info("retention.skipped", reason="disabled")
        return {}

    moment = now or datetime.now(UTC)
    deleted: dict[str, int] = {}

    for policy in policies or policies_from_config(config):
        model, timestamp_column = _PURGE_TARGETS[policy.name]
        cutoff = policy.cutoff(moment)
        total = 0

        while True:
            # Select a batch of ids, then delete exactly those rows: PostgreSQL
            # has no DELETE ... LIMIT.
            batch = (
                (
                    await session.execute(
                        select(model.id)
                        .where(timestamp_column < cutoff)
                        .limit(config.purge_batch_size)
                    )
                )
                .scalars()
                .all()
            )
            if not batch:
                break
            await session.execute(delete(model).where(model.id.in_(batch)))
            total += len(batch)
            if len(batch) < config.purge_batch_size:
                break

        deleted[policy.name] = total
        if total:
            logger.info(
                "retention.purged",
                table=policy.name,
                rows=total,
                cutoff=cutoff.isoformat(),
            )

    return deleted
