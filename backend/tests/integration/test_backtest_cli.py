"""``trading-bot-backtest``: arguments, overrides, and the inspect commands."""

from __future__ import annotations

import argparse
import json
import uuid
from collections.abc import Iterator
from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tests.integration.backtest_support import (
    PAIR,
    PERP,
    START,
    DatasetBuilder,
    backtest_settings,
    cleanup,
    run_async,
    seed,
)
from tests.integration.conftest import _HOST, _PASSWORD, _PORT, _USER, TEST_DATABASE
from trading_bot.backtest.cli import EXIT_CODES, apply_overrides, main, parse_market, parse_time
from trading_bot.backtest.engine import BacktestEngine
from trading_bot.backtest.service import run_backtest
from trading_bot.core.config import LoggingConfig, Settings
from trading_bot.core.logging import configure_logging
from trading_bot.db.models import BacktestRun
from trading_bot.db.models.enums import BacktestRunStatus, MarketType

pytestmark = pytest.mark.requires_postgres


@pytest.fixture(scope="module", autouse=True)
def _logging_outside_capsys() -> None:
    """``main`` configures logging once per process. Doing it first here, outside
    any ``capsys`` test, keeps the exception printer off a stream that
    ``capsys`` closes - a later test's ``logger.exception`` would write to it."""
    configure_logging(LoggingConfig())


class TestArguments:
    def test_times_need_a_timezone(self) -> None:
        assert parse_time("2026-09-11T08:00:00Z").utcoffset() == timedelta(0)
        with pytest.raises(argparse.ArgumentTypeError):
            parse_time("2026-09-11T08:00:00")

    def test_markets_are_venue_symbol_type(self) -> None:
        ref = parse_market("binance:ethusdt:perpetual")
        assert (ref.venue, ref.symbol, ref.market_type) == (
            "binance",
            "ETHUSDT",
            MarketType.PERPETUAL,
        )
        with pytest.raises(argparse.ArgumentTypeError):
            parse_market("ETHUSDT")

    def test_overrides_are_validated_like_any_configuration(self) -> None:
        settings = apply_overrides(
            Settings(), ["strategy.spot_perp_basis.min_net_edge_bps=5", "execution.latency_ms=40"]
        )
        assert settings.strategy.spot_perp_basis.min_net_edge_bps == 5
        assert settings.execution.latency_ms == 40
        with pytest.raises(ValueError, match="unknown setting"):
            apply_overrides(Settings(), ["strategy.no_such_thing=1"])
        with pytest.raises(ValueError):  # the timing validator still runs
            apply_overrides(Settings(), ["execution.timeout_ms=10"])

    def test_exit_codes_distinguish_every_terminal_status(self) -> None:
        assert len(set(EXIT_CODES.values())) == len(EXIT_CODES) == 4


@pytest.fixture
def cli_database(postgres_url: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for key, value in {
        "TB_DATABASE__HOST": _HOST,
        "TB_DATABASE__PORT": _PORT,
        "TB_DATABASE__USER": _USER,
        "TB_DATABASE__PASSWORD": _PASSWORD,
        "TB_DATABASE__NAME": TEST_DATABASE,
        "TB_EXCHANGE__VENUE": "replaytest",
    }.items():
        monkeypatch.setenv(key, value)
    cleanup()
    yield
    cleanup()


@pytest.mark.usefixtures("cli_database")
class TestInspect:
    def _finished_run(self) -> uuid.UUID:
        dataset = DatasetBuilder()
        dataset.observe_funding(PERP, START)
        for second in range(5):
            dataset.market_pair(
                START + timedelta(seconds=second), spot_mid=Decimal(100_000), basis_bps=Decimal(1)
            )
        seed(dataset)
        outcome = run_backtest(
            backtest_settings(), start=START, end=START + timedelta(seconds=4), refs=PAIR
        )
        return outcome.run.run_uid

    def test_status_and_report_read_a_finished_run(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        uid = self._finished_run()
        with pytest.raises(SystemExit) as status:
            main(["status", str(uid)])
        assert status.value.code == 0
        assert "COMPLETED" in capsys.readouterr().out

        with pytest.raises(SystemExit) as report:
            main(["report", str(uid), "--json"])
        assert report.value.code == 0
        document = json.loads(capsys.readouterr().out)
        assert document["run_uid"] == str(uid)
        assert document["trade_count"] == 1  # 1 bps clears a 1 bps floor at zero fees
        assert document["pnl_complete"] is True

        with pytest.raises(SystemExit) as text:
            main(["report", str(uid)])
        assert text.value.code == 0
        assert "Sharpe / Sortino" in capsys.readouterr().out

    def test_cancelling_a_pending_run_finishes_it_and_a_finished_one_is_refused(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        async def pending(session: AsyncSession) -> uuid.UUID:
            run = BacktestRun(
                run_uid=uuid.uuid4(),
                dataset_source="postgres",
                requested_start=START,
                requested_end=START + timedelta(minutes=1),
                markets=[],
                config_snapshot={},
                config_hash="0" * 64,
            )
            session.add(run)
            await session.flush()
            return run.run_uid

        uid = run_async(pending)
        with pytest.raises(SystemExit) as cancelled:
            main(["cancel", str(uid)])
        assert cancelled.value.code == 0

        async def read(session: AsyncSession) -> BacktestRun:
            return (
                await session.execute(select(BacktestRun).where(BacktestRun.run_uid == uid))
            ).scalar_one()

        row = run_async(read)
        assert row.status is BacktestRunStatus.CANCELLED and row.completed_at is not None

        finished = self._finished_run()
        capsys.readouterr()
        with pytest.raises(SystemExit) as refused:
            main(["cancel", str(finished)])
        assert refused.value.code == 1

    def test_unknown_runs_exit_nonzero(self) -> None:
        for command in ("status", "cancel", "report"):
            with pytest.raises(SystemExit) as outcome:
                main([command, str(uuid.uuid4())])
            assert outcome.value.code == 1


@pytest.mark.usefixtures("cli_database")
def test_run_announces_its_uid_before_replaying_and_keeps_stdout_parseable(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    dataset = DatasetBuilder()
    dataset.observe_funding(PERP, START)
    for second in range(5):
        dataset.market_pair(
            START + timedelta(seconds=second), spot_mid=Decimal(100_000), basis_bps=Decimal(0)
        )
    seed(dataset)
    seen_before_replay: list[str] = []
    original = BacktestEngine.run

    async def spy(self: BacktestEngine, run: Any) -> Any:
        seen_before_replay.append(capsys.readouterr().err)
        return await original(self, run)

    monkeypatch.setattr(BacktestEngine, "run", spy)
    with pytest.raises(SystemExit):
        main(
            [
                "run",
                "--start",
                START.isoformat(),
                "--end",
                (START + timedelta(seconds=4)).isoformat(),
                "--market",
                f"replaytest:BTCUSDT:{MarketType.SPOT.value}",
                "--market",
                f"replaytest:BTCUSDT:{PAIR[1].market_type.value}",
                "--json",
            ]
        )
    document = json.loads(capsys.readouterr().out)
    (announcement,) = seen_before_replay
    assert f"backtest {document['run_uid']} created" in announcement
