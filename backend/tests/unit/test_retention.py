"""Retention policy logic (no database required)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from trading_bot.core.config import RetentionConfig, Settings
from trading_bot.db.retention import RetentionPolicy, policies_from_config

NOW = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)


class TestPolicyDerivation:
    def test_policies_follow_configuration(self) -> None:
        config = RetentionConfig(market_data_days=3, order_books_days=1, trades_market_days=5)
        assert policies_from_config(config) == (
            RetentionPolicy("market_data", 3),
            RetentionPolicy("order_books", 1),
            RetentionPolicy("trades_market", 5),
        )

    def test_cutoff_is_now_minus_window(self) -> None:
        assert RetentionPolicy("market_data", 7).cutoff(NOW) == datetime(
            2026, 9, 4, 12, 0, tzinfo=UTC
        )

    def test_only_raw_feeds_are_purged(self) -> None:
        """Opportunities, orders, fills and P&L are never on the purge list."""
        names = {policy.name for policy in policies_from_config(RetentionConfig())}
        assert names == {"market_data", "order_books", "trades_market"}
        assert not names & {
            "opportunities",
            "signals",
            "orders",
            "fills",
            "positions",
            "pnl_snapshots",
            "risk_events",
            "system_events",
        }


class TestConfiguration:
    def test_defaults_keep_a_working_window(self) -> None:
        retention = Settings().retention
        assert retention.enabled is True
        assert retention.market_data_days == 7
        # Depth snapshots are the largest rows, so they expire soonest.
        assert retention.order_books_days < retention.market_data_days

    def test_windows_must_be_at_least_a_day(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            RetentionConfig(market_data_days=0)

    def test_batch_size_has_a_sane_floor(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            RetentionConfig(purge_batch_size=1)

    def test_retention_is_immutable(self) -> None:
        from pydantic import ValidationError

        config = RetentionConfig()
        with pytest.raises(ValidationError):
            config.market_data_days = 99  # type: ignore[misc]
