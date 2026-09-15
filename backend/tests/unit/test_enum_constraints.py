"""Every enum column is enforced by the database, not only by the ORM.

``enum_types`` documented a CHECK constraint for years before it created one.
These tests fail if a column loses it, if its allowed values drift from the
Python enum, or if two columns in one table would give their constraints the
same name - which ``create_constraint=True`` does when a table uses one enum
type twice.
"""

from __future__ import annotations

import re
from collections import Counter

import pytest
from sqlalchemy import CheckConstraint, Enum, Table
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

import trading_bot.db.models  # noqa: F401  (registers tables)
from trading_bot.db.base import Base
from trading_bot.db.models.enums import ExecutionMode
from trading_bot.db.scope import RunScope

ENUM_COLUMNS = [
    (table, column)
    for table in sorted(Base.metadata.tables.values(), key=lambda t: t.name)
    for column in table.columns
    if isinstance(column.type, Enum)
]


def _ddl(table: Table) -> str:
    return str(CreateTable(table).compile(dialect=postgresql.dialect()))


@pytest.mark.parametrize(
    ("table", "column"), ENUM_COLUMNS, ids=lambda item: getattr(item, "name", str(item))
)
def test_every_enum_column_has_a_check_listing_exactly_its_values(
    table: Table, column: object
) -> None:
    enum_type = column.type  # type: ignore[attr-defined]
    name = f"ck_{table.name}_{enum_type.name}"
    match = re.search(rf"CONSTRAINT {name} CHECK \((\w+) IN \(([^)]*)\)\)", _ddl(table))
    assert match is not None, f"{table.name}.{column.name} has no {name} constraint"  # type: ignore[attr-defined]
    assert match.group(1) == column.name  # type: ignore[attr-defined]
    allowed = [value.strip().strip("'") for value in match.group(2).split(",")]
    assert allowed == list(enum_type.enums)


@pytest.mark.parametrize(
    "table", sorted(Base.metadata.tables.values(), key=lambda t: t.name), ids=lambda t: t.name
)
def test_check_constraint_names_are_unique_per_table(table: Table) -> None:
    names = Counter(
        constraint.name
        for constraint in table.constraints
        if isinstance(constraint, CheckConstraint) and constraint.name is not None
    )
    duplicated = [name for name, count in names.items() if count > 1]
    assert not duplicated, f"{table.name} would create duplicate constraints {duplicated}"


def test_every_table_with_a_mode_ties_backtest_rows_to_a_run() -> None:
    for table in Base.metadata.tables.values():
        if "mode" not in table.columns:
            continue
        assert "backtest_run_id" in table.columns, table.name
        assert f"CONSTRAINT ck_{table.name}_backtest_mode_has_run" in _ddl(table), table.name


class TestRunScope:
    def test_a_backtest_scope_needs_a_run(self) -> None:
        with pytest.raises(ValueError, match="BACKTEST"):
            RunScope(ExecutionMode.BACKTEST)

    def test_only_a_backtest_scope_may_have_a_run(self) -> None:
        with pytest.raises(ValueError, match="BACKTEST"):
            RunScope(ExecutionMode.PAPER, 7)

    def test_outside_a_run_the_filter_is_is_null_never_any_run(self) -> None:
        from trading_bot.db.models import Order

        clause = RunScope(ExecutionMode.PAPER).run_filter(Order.backtest_run_id)
        assert str(clause.compile(dialect=postgresql.dialect())) == "orders.backtest_run_id IS NULL"
        scoped = RunScope.backtest(3).values()
        assert scoped == {"mode": ExecutionMode.BACKTEST, "backtest_run_id": 3}
