"""Schema invariants checked against the model metadata.

These run without a database: they assert the design rules that are easy to
break accidentally when adding a table later - money stored as NUMERIC,
timestamps timezone-aware, every table indexed on the column it is queried by.
"""

from __future__ import annotations

import pytest
from sqlalchemy import DateTime, Float, Numeric, Table

import trading_bot.db.models  # noqa: F401  (registers tables)
from trading_bot.db.base import Base
from trading_bot.db.models import enums

EXPECTED_TABLES = {
    "markets",
    "market_data",
    "order_books",
    "trades_market",
    "opportunities",
    "signals",
    "orders",
    "fills",
    "positions",
    "portfolio_snapshots",
    "pnl_snapshots",
    "risk_events",
    "system_events",
}

TABLES = Base.metadata.tables
ALL_TABLES = sorted(TABLES.values(), key=lambda t: t.name)

# Ratios and statistics may be floats; money and prices may not.
FLOAT_ALLOWED = {
    "pnl_snapshots": {
        "win_rate",
        "profit_factor",
        "expectancy_usd",
        "sharpe_ratio",
        "sortino_ratio",
        "total_return_pct",
    }
}


class TestTables:
    def test_every_required_table_exists(self) -> None:
        assert set(TABLES) >= EXPECTED_TABLES

    def test_no_unexpected_tables(self) -> None:
        assert set(TABLES) == EXPECTED_TABLES

    @pytest.mark.parametrize("table", ALL_TABLES, ids=lambda t: t.name)
    def test_table_has_a_primary_key(self, table: Table) -> None:
        assert list(table.primary_key.columns), table.name

    @pytest.mark.parametrize("table", ALL_TABLES, ids=lambda t: t.name)
    def test_table_has_audit_timestamps(self, table: Table) -> None:
        assert {"created_at", "updated_at"} <= set(table.c.keys()), table.name


class TestNumericDiscipline:
    """Money must never be a float - rounding drift in an audit trail is a bug."""

    @pytest.mark.parametrize("table", ALL_TABLES, ids=lambda t: t.name)
    def test_no_floats_for_money(self, table: Table) -> None:
        allowed = FLOAT_ALLOWED.get(table.name, set())
        offenders = [
            column.name
            for column in table.c
            if isinstance(column.type, Float) and column.name not in allowed
        ]
        assert offenders == [], f"{table.name} stores {offenders} as float"

    def test_price_columns_use_full_precision(self) -> None:
        price = TABLES["market_data"].c.bid.type
        assert isinstance(price, Numeric)
        assert (price.precision, price.scale) == (28, 12)

    def test_money_columns_use_money_scale(self) -> None:
        money = TABLES["opportunities"].c.net_edge_usd.type
        assert isinstance(money, Numeric)
        assert (money.precision, money.scale) == (20, 8)


class TestTimestamps:
    @pytest.mark.parametrize("table", ALL_TABLES, ids=lambda t: t.name)
    def test_all_timestamps_are_timezone_aware(self, table: Table) -> None:
        naive = [
            column.name
            for column in table.c
            if isinstance(column.type, DateTime) and not column.type.timezone
        ]
        assert naive == [], f"{table.name} has naive timestamps: {naive}"

    def test_market_data_records_both_clocks(self) -> None:
        """Latency is measured, not guessed."""
        columns = set(TABLES["market_data"].c.keys())
        assert {"exchange_timestamp", "local_timestamp", "latency_ms"} <= columns


class TestEnumColumns:
    def test_enums_are_checked_varchars_not_native_types(self) -> None:
        """Native PG enums would need ALTER TYPE to add a status later."""
        from sqlalchemy import Enum as SaEnum

        enum_columns = [
            (table.name, column.name, column.type)
            for table in ALL_TABLES
            for column in table.c
            if isinstance(column.type, SaEnum)
        ]
        assert enum_columns
        for table_name, column_name, column_type in enum_columns:
            assert column_type.native_enum is False, f"{table_name}.{column_name}"

    def test_execution_mode_separates_theoretical_paper_and_live(self) -> None:
        assert [m.value for m in enums.ExecutionMode] == ["THEORETICAL", "PAPER", "LIVE"]

    def test_opportunity_statuses_cover_the_full_lifecycle(self) -> None:
        assert {m.value for m in enums.OpportunityStatus} == {
            "DETECTED",
            "VALIDATED",
            "REJECTED",
            "PAPER_TRADE",
            "EXPIRED",
            "EXECUTED",
            "FAILED",
        }


class TestTraceabilityWiring:
    """Each stage must point at the stage that caused it."""

    @pytest.mark.parametrize(
        ("table", "column", "target"),
        [
            ("market_data", "market_id", "markets.id"),
            ("opportunities", "market_data_id", "market_data.id"),
            ("opportunities", "secondary_market_data_id", "market_data.id"),
            ("signals", "opportunity_id", "opportunities.id"),
            ("risk_events", "signal_id", "signals.id"),
            ("orders", "signal_id", "signals.id"),
            ("orders", "risk_event_id", "risk_events.id"),
            ("fills", "order_id", "orders.id"),
            ("fills", "position_id", "positions.id"),
            ("positions", "opportunity_id", "opportunities.id"),
            ("pnl_snapshots", "position_id", "positions.id"),
        ],
    )
    def test_foreign_key_exists(self, table: str, column: str, target: str) -> None:
        targets = {fk.target_fullname for fk in TABLES[table].c[column].foreign_keys}
        assert target in targets, f"{table}.{column} -> {target} missing"

    def test_mode_is_recorded_wherever_results_are(self) -> None:
        """Paper and live rows must be distinguishable in every result table."""
        for name in ("orders", "fills", "positions", "portfolio_snapshots", "pnl_snapshots"):
            assert "mode" in TABLES[name].c, name


class TestIndexes:
    @pytest.mark.parametrize(
        ("table", "column"),
        [
            ("market_data", "local_timestamp"),
            ("order_books", "local_timestamp"),
            ("trades_market", "local_timestamp"),
        ],
    )
    def test_retention_columns_are_indexed(self, table: str, column: str) -> None:
        """Purges filter on these; an unindexed scan would lock the table."""
        indexed = {c.name for index in TABLES[table].indexes for c in index.columns}
        assert column in indexed, f"{table}.{column} not indexed"

    def test_opportunity_research_queries_are_indexed(self) -> None:
        indexed = {
            tuple(c.name for c in index.columns) for index in TABLES["opportunities"].indexes
        }
        assert ("detected_at",) in indexed
        assert ("net_edge_bps",) in indexed
        assert ("status", "detected_at") in indexed

    def test_duplicate_protection_constraints_exist(self) -> None:
        """Idempotency keys: a retry must not create a second order or fill."""
        order_uniques = {
            tuple(c.name for c in constraint.columns)
            for constraint in TABLES["orders"].constraints
            if constraint.__class__.__name__ == "UniqueConstraint"
        }
        assert ("mode", "client_order_id") in order_uniques

        trade_uniques = {
            tuple(c.name for c in constraint.columns)
            for constraint in TABLES["trades_market"].constraints
            if constraint.__class__.__name__ == "UniqueConstraint"
        }
        assert ("market_id", "exchange_trade_id") in trade_uniques
