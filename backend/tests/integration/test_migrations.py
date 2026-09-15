"""The migration must produce exactly the schema the models describe.

Without this test, a model change that nobody generated a migration for would
pass every other test (they build the schema from metadata) and then fail on a
real deployment. Here the migration is applied to a scratch database and the
result is compared against the model metadata.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

import trading_bot.db.models  # noqa: F401  (registers tables)
from tests.integration.conftest import _HOST, _PASSWORD, _PORT, _USER
from trading_bot.db.base import Base

pytestmark = pytest.mark.requires_postgres

MIGRATION_DATABASE = "trading_bot_migration_check"
BACKEND_DIR = Path(__file__).resolve().parents[2]
ADMIN_URL = f"postgresql+asyncpg://{_USER}:{_PASSWORD}@{_HOST}:{_PORT}/postgres"
TARGET_URL = f"postgresql+asyncpg://{_USER}:{_PASSWORD}@{_HOST}:{_PORT}/{MIGRATION_DATABASE}"


async def _recreate_database() -> None:
    engine = create_async_engine(ADMIN_URL, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as connection:
            await connection.execute(text(f'DROP DATABASE IF EXISTS "{MIGRATION_DATABASE}"'))
            await connection.execute(text(f'CREATE DATABASE "{MIGRATION_DATABASE}"'))
    finally:
        await engine.dispose()


def _run_alembic(*args: str) -> subprocess.CompletedProcess[str]:
    """Run alembic in a subprocess so env.py resolves settings independently."""
    environment = {
        **os.environ,
        "TB_PROFILE": "development",
        "TB_DATABASE__HOST": _HOST,
        "TB_DATABASE__PORT": _PORT,
        "TB_DATABASE__USER": _USER,
        "TB_DATABASE__PASSWORD": _PASSWORD,
        "TB_DATABASE__NAME": MIGRATION_DATABASE,
    }
    return subprocess.run(  # noqa: S603 - fixed argv, no shell, test-only
        [sys.executable, "-m", "alembic", *args],
        cwd=BACKEND_DIR,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture(scope="module")
def migrated_database(postgres_url: str) -> Iterator[str]:
    """A database built purely by running the migrations."""
    asyncio.run(_recreate_database())
    result = _run_alembic("upgrade", "head")
    assert result.returncode == 0, f"alembic upgrade failed:\n{result.stderr}"
    yield TARGET_URL


class TestMigrationParity:
    async def test_migrated_schema_matches_the_models(self, migrated_database: str) -> None:
        """No pending model changes: autogenerate finds nothing to do."""
        engine = create_async_engine(migrated_database)

        def _diff(connection: Connection) -> list[object]:
            context = MigrationContext.configure(
                connection,
                opts={"compare_type": True, "compare_server_default": True},
            )
            return compare_metadata(context, Base.metadata)

        try:
            async with engine.connect() as connection:
                differences = await connection.run_sync(_diff)
        finally:
            await engine.dispose()

        assert differences == [], f"models and migrations disagree: {differences}"

    async def test_every_table_is_created_by_the_migration(self, migrated_database: str) -> None:
        engine = create_async_engine(migrated_database)
        try:
            async with engine.connect() as connection:
                rows = await connection.execute(
                    text(
                        "SELECT table_name FROM information_schema.tables "
                        "WHERE table_schema = 'public'"
                    )
                )
                tables = {row[0] for row in rows}
        finally:
            await engine.dispose()

        assert set(Base.metadata.tables) <= tables
        assert "alembic_version" in tables


class TestDowngrade:
    def test_downgrade_removes_everything_it_created(self, migrated_database: str) -> None:
        """A migration that cannot be undone is a one-way door."""
        downgrade = _run_alembic("downgrade", "base")
        assert downgrade.returncode == 0, downgrade.stderr

        async def _remaining() -> set[str]:
            engine = create_async_engine(TARGET_URL)
            try:
                async with engine.connect() as connection:
                    rows = await connection.execute(
                        text(
                            "SELECT table_name FROM information_schema.tables "
                            "WHERE table_schema = 'public'"
                        )
                    )
                    return {row[0] for row in rows}
            finally:
                await engine.dispose()

        tables = asyncio.run(_remaining())
        # alembic_version is alembic's own bookkeeping and stays behind.
        assert tables == {"alembic_version"}

        # Leave the scratch database usable for any later run.
        assert _run_alembic("upgrade", "head").returncode == 0


def _metadata_ddl() -> tuple[
    set[tuple[str, str]], set[tuple[str, str, str, bool]], set[tuple[str, str, bool, str, str]]
]:
    """CHECK names, UNIQUE constraints and indexes exactly as the models would create them."""
    import re

    from sqlalchemy.dialects import postgresql
    from sqlalchemy.schema import CreateIndex, CreateTable

    dialect = postgresql.dialect()
    checks: set[tuple[str, str]] = set()
    uniques: set[tuple[str, str, str, bool]] = set()
    indexes: set[tuple[str, str, bool, str, str]] = set()
    for table in Base.metadata.tables.values():
        ddl = str(CreateTable(table).compile(dialect=dialect))
        checks |= {(table.name, name) for name in re.findall(r"CONSTRAINT (\w+) CHECK", ddl)}
        for name, not_distinct, columns in re.findall(
            r"CONSTRAINT (\w+) UNIQUE( NULLS NOT DISTINCT)? \(([^)]*)\)", ddl
        ):
            uniques.add((table.name, name, columns.replace(" ", ""), bool(not_distinct)))
        for index in table.indexes:
            text_ = str(CreateIndex(index).compile(dialect=dialect))
            match = re.search(r"\(([^)]*)\)(?: WHERE (.*))?$", text_)
            assert match is not None
            predicate = (match.group(2) or "").replace("(", "").replace(")", "").strip()
            indexes.add(
                (
                    table.name,
                    str(index.name),
                    bool(index.unique),
                    match.group(1).replace(" ", ""),
                    predicate,
                )
            )
    return checks, uniques, indexes


class TestCatalogParity:
    """What ``compare_metadata`` cannot see: CHECKs, NULLS NOT DISTINCT, index predicates."""

    async def test_constraints_and_indexes_match_the_models(self, migrated_database: str) -> None:
        import re

        checks, uniques, indexes = _metadata_ddl()
        engine = create_async_engine(migrated_database)
        try:
            async with engine.connect() as connection:
                db_checks = {
                    (row[0], row[1])
                    for row in await connection.execute(
                        text(
                            "SELECT conrelid::regclass::text, conname FROM pg_constraint "
                            "WHERE contype = 'c' AND connamespace = 'public'::regnamespace"
                        )
                    )
                }
                db_uniques = set()
                for table, name, definition in await connection.execute(
                    text(
                        "SELECT conrelid::regclass::text, conname, pg_get_constraintdef(oid) "
                        "FROM pg_constraint WHERE contype = 'u' "
                        "AND connamespace = 'public'::regnamespace"
                    )
                ):
                    match = re.match(r"UNIQUE( NULLS NOT DISTINCT)? \(([^)]*)\)", definition)
                    assert match is not None, definition
                    db_uniques.add(
                        (table, name, match.group(2).replace(" ", ""), bool(match.group(1)))
                    )
                db_indexes = set()
                for table, name, definition in await connection.execute(
                    text(
                        "SELECT i.tablename, i.indexname, i.indexdef FROM pg_indexes i "
                        "WHERE i.schemaname = 'public' AND NOT EXISTS "
                        "(SELECT 1 FROM pg_constraint c WHERE c.conname = i.indexname) "
                        "AND i.tablename <> 'alembic_version'"
                    )
                ):
                    match = re.search(r"USING btree \(([^)]*)\)(?: WHERE (.*))?$", definition)
                    assert match is not None, definition
                    predicate = (match.group(2) or "").replace("(", "").replace(")", "").strip()
                    db_indexes.add(
                        (
                            table,
                            name,
                            "UNIQUE INDEX" in definition,
                            match.group(1).replace(" ", ""),
                            predicate,
                        )
                    )
        finally:
            await engine.dispose()

        assert db_checks == checks
        assert db_uniques == uniques
        assert db_indexes == indexes

    async def test_enum_checks_list_the_current_values(self, migrated_database: str) -> None:
        engine = create_async_engine(migrated_database)
        try:
            async with engine.connect() as connection:
                definition = await connection.scalar(
                    text(
                        "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                        "WHERE conname = 'ck_orders_execution_mode'"
                    )
                )
        finally:
            await engine.dispose()
        for value in ("THEORETICAL", "PAPER", "LIVE", "BACKTEST"):
            assert f"'{value}'" in definition


# Fixed test literals, formatted only with constants below - never input.
LEGACY_MARKET = (
    "INSERT INTO markets (venue, symbol, market_type, base_asset, quote_asset, is_active, "
    "is_monitored) VALUES ('legacy', '{symbol}', '{market_type}', 'X', 'USDT', true, false)"
)
LEGACY_ORDER = (
    "INSERT INTO orders (market_id, mode, client_order_id, side, order_type, quantity, "
    "filled_quantity, status) SELECT id, 'PAPER', '{client}', 'BUY', 'MARKET', 1, 0, 'FILLED' "
    "FROM markets WHERE venue = 'legacy'"
)


async def _execute(url: str, *statements: str) -> list[Any]:
    engine = create_async_engine(url)
    results: list[Any] = []
    try:
        async with engine.begin() as connection:
            for statement in statements:
                result = await connection.execute(text(statement))
                results.append(result.scalar() if result.returns_rows else None)
    finally:
        await engine.dispose()
    return results


class TestPhase11Migrations:
    def test_downgrade_refuses_to_delete_backtests_unless_asked(
        self, migrated_database: str
    ) -> None:
        asyncio.run(
            _execute(
                migrated_database,
                "INSERT INTO backtest_runs (run_uid, dataset_source, requested_start, "
                "requested_end, markets, config_snapshot, config_hash) VALUES "
                "(gen_random_uuid(), 'postgres', now(), now() + interval '1 hour', '[]', '{}', "
                "repeat('0', 64))",
            )
        )
        refused = _run_alembic("downgrade", "b5d9e2c4a7f1")
        assert refused.returncode != 0
        assert "refusing to downgrade" in refused.stderr
        assert asyncio.run(_execute(migrated_database, "SELECT count(*) FROM backtest_runs")) == [1]

        purged = _run_alembic("-x", "purge_backtests=true", "downgrade", "b5d9e2c4a7f1")
        assert purged.returncode == 0, purged.stderr
        assert _run_alembic("upgrade", "head").returncode == 0
        assert asyncio.run(_execute(migrated_database, "SELECT count(*) FROM backtest_runs")) == [0]

    def test_enum_constraints_refuse_to_install_over_invalid_rows(
        self, migrated_database: str
    ) -> None:
        assert _run_alembic("downgrade", "e4f70b2c8d13").returncode == 0
        asyncio.run(
            _execute(
                migrated_database,
                LEGACY_MARKET.format(symbol="XUSDT", market_type="SWAP"),
            )
        )
        failed = _run_alembic("upgrade", "head")
        assert failed.returncode != 0
        assert "markets.market_type: 'SWAP' x1" in failed.stderr
        asyncio.run(_execute(migrated_database, "DELETE FROM markets WHERE venue = 'legacy'"))
        assert _run_alembic("upgrade", "head").returncode == 0

    def test_legacy_rows_survive_the_upgrade(self, migrated_database: str) -> None:
        assert _run_alembic("downgrade", "e4f70b2c8d13").returncode == 0
        asyncio.run(
            _execute(
                migrated_database,
                LEGACY_MARKET.format(symbol="YUSDT", market_type="SPOT"),
                LEGACY_ORDER.format(client="legacy-0"),
                LEGACY_ORDER.format(client="legacy-1"),
            )
        )
        upgraded = _run_alembic("upgrade", "head")
        assert upgraded.returncode == 0, upgraded.stderr
        counts = asyncio.run(
            _execute(
                migrated_database,
                "SELECT count(*) FROM orders WHERE client_order_id LIKE 'legacy-%' "
                "AND backtest_run_id IS NULL AND execution_intent_id IS NULL",
                "DELETE FROM orders WHERE client_order_id LIKE 'legacy-%'",
                "DELETE FROM markets WHERE venue = 'legacy'",
            )
        )
        assert counts[0] == 2


def _metadata_foreign_keys() -> set[tuple[str, str, str]]:
    import re

    from sqlalchemy.dialects import postgresql
    from sqlalchemy.schema import CreateTable

    keys: set[tuple[str, str, str]] = set()
    for table in Base.metadata.tables.values():
        ddl = str(CreateTable(table).compile(dialect=postgresql.dialect()))
        for name, definition in re.findall(
            r"CONSTRAINT (\w+) (FOREIGN KEY.*?),?\s*$", ddl, flags=re.MULTILINE
        ):
            keys.add((table.name, name, _normalise_fk(definition)))
    return keys


def _normalise_fk(definition: str) -> str:
    import re

    compact = re.sub(r"\s+", "", definition).replace('"', "").lower()
    return compact.replace("ondeletenoaction", "")


class TestRelationalParity:
    async def test_foreign_keys_and_run_key_triggers_match_the_models(
        self, migrated_database: str
    ) -> None:
        from trading_bot.db.models.backtest import RUN_KEYED_TABLES

        engine = create_async_engine(migrated_database)
        try:
            async with engine.connect() as connection:
                keys = {
                    (table, name, _normalise_fk(definition))
                    for table, name, definition in await connection.execute(
                        text(
                            "SELECT conrelid::regclass::text, conname, pg_get_constraintdef(oid) "
                            "FROM pg_constraint WHERE contype = 'f' "
                            "AND connamespace = 'public'::regnamespace"
                        )
                    )
                }
                triggers = {
                    row[0]
                    for row in await connection.execute(
                        text(
                            "SELECT tgrelid::regclass::text FROM pg_trigger "
                            "WHERE NOT tgisinternal AND tgname LIKE 'trg_%_run_key'"
                        )
                    )
                }
        finally:
            await engine.dispose()
        assert keys == _metadata_foreign_keys()
        assert triggers == set(RUN_KEYED_TABLES)

    def test_linked_rows_survive_upgrade_and_downgrade_of_the_run_keys(
        self, migrated_database: str
    ) -> None:
        assert _run_alembic("downgrade", "c8e1f3a5b9d2").returncode == 0
        asyncio.run(
            _execute(
                migrated_database,
                LEGACY_MARKET.format(symbol="ZUSDT", market_type="SPOT"),
                "INSERT INTO backtest_runs (run_uid, dataset_source, requested_start, "
                "requested_end, markets, config_snapshot, config_hash) VALUES "
                "(gen_random_uuid(), 'postgres', now(), now() + interval '1 hour', '[]', '{}', "
                "repeat('0', 64))",
                LEGACY_ORDER.format(client="linked-paper"),
                "INSERT INTO orders (market_id, mode, backtest_run_id, client_order_id, side, "
                "order_type, quantity, filled_quantity, status) SELECT m.id, 'BACKTEST', r.id, "
                "'linked-run', 'BUY', 'MARKET', 1, 1, 'FILLED' FROM markets m, backtest_runs r "
                "WHERE m.venue = 'legacy'",
                "INSERT INTO fills (order_id, mode, backtest_run_id, price, quantity, fee_usd, "
                "filled_at, fill_index) SELECT id, mode, backtest_run_id, 1, 1, 0, now(), 0 "
                "FROM orders WHERE client_order_id LIKE 'linked-%'",
            )
        )
        upgraded = _run_alembic("upgrade", "head")
        assert upgraded.returncode == 0, upgraded.stderr
        linked = asyncio.run(
            _execute(
                migrated_database,
                "SELECT count(*) FROM fills f JOIN orders o ON o.id = f.order_id "
                "AND o.run_key = f.run_key WHERE o.client_order_id LIKE 'linked-%' "
                "AND f.run_key = COALESCE(f.backtest_run_id, 0)",
            )
        )
        assert linked == [2]
        down = _run_alembic("-x", "purge_backtests=true", "downgrade", "c8e1f3a5b9d2")
        assert down.returncode == 0, down.stderr
        survived = asyncio.run(
            _execute(
                migrated_database,
                "SELECT count(*) FROM fills f JOIN orders o ON o.id = f.order_id "
                "WHERE o.client_order_id LIKE 'linked-%'",
            )
        )
        assert survived == [2], "a run_key downgrade loses no row"
        assert _run_alembic("upgrade", "head").returncode == 0
        asyncio.run(
            _execute(
                migrated_database,
                "DELETE FROM backtest_runs",
                "DELETE FROM orders WHERE client_order_id LIKE 'linked-%'",
                "DELETE FROM markets WHERE venue = 'legacy'",
            )
        )
