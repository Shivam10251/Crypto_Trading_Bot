"""``trading-bot-risk``: the operator's only way to flip the kill switch.

The exit code is the contract. An operator - or a deploy script - that runs
``trading-bot-risk kill`` and gets a zero back will believe trading has
stopped. If the row never reached the database, no running service will ever
observe it, so a silent success there is the worst possible failure mode.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from trading_bot import cli
from trading_bot.core.config import Settings
from trading_bot.db.models.enums import ExecutionMode, RiskDecision, RiskEventType
from trading_bot.risk.models import RiskEventDraft, RiskVerdict

NOW = datetime(2026, 9, 12, 12, 0, tzinfo=UTC)


def _verdict(risk_event_id: int | None, decision: RiskDecision) -> RiskVerdict:
    return RiskVerdict(
        RiskEventDraft(
            occurred_at=NOW,
            event_type=RiskEventType.KILL_SWITCH,
            decision=decision,
            mode=ExecutionMode.PAPER,
            intent_id="kill_switch:test",
            reason="test",
        ),
        risk_event_id,
    )


class FakeSwitch:
    """Stands in for ``KillSwitchState`` with a configurable durability."""

    durable: bool = True
    engaged: bool = False

    def __init__(self, _store: Any, _factory: Any, **_kwargs: Any) -> None:
        pass

    async def load(self) -> None:
        return None

    @property
    def is_active(self) -> bool:
        return type(self).engaged

    def blocked_reason(self) -> str | None:
        return "halted" if type(self).engaged else None

    async def trigger(self, *, who: str, reason: str, **_kwargs: Any) -> RiskVerdict:
        return _verdict(1 if type(self).durable else None, RiskDecision.PAUSED)

    async def rearm(self, *, who: str, reason: str) -> RiskVerdict:
        return _verdict(2 if type(self).durable else None, RiskDecision.APPROVED)


@pytest.fixture(autouse=True)
def cli_harness(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeSwitch.durable = True
    FakeSwitch.engaged = False
    monkeypatch.setattr(cli, "get_settings", Settings)
    monkeypatch.setattr(cli, "configure_logging", lambda _config: None)
    monkeypatch.setattr(cli, "init_engine", lambda _config: None)

    async def _dispose() -> None:
        return None

    monkeypatch.setattr(cli, "dispose_engine", _dispose)
    monkeypatch.setattr(cli, "RiskEventStore", lambda _factory: object())
    monkeypatch.setattr(cli, "KillSwitchState", FakeSwitch)


def run(monkeypatch: pytest.MonkeyPatch, *argv: str) -> None:
    monkeypatch.setattr("sys.argv", ["trading-bot-risk", *argv])
    cli.run_risk_control()


class TestExitCodes:
    def test_a_kill_that_was_not_recorded_exits_nonzero(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        FakeSwitch.durable = False
        with pytest.raises(SystemExit) as exit_info:
            run(monkeypatch, "kill", "--who", "operator", "--reason", "halt")
        assert exit_info.value.code == 1
        out = capsys.readouterr().out
        assert "NOT durably recorded" in out
        assert "will NOT observe this kill" in out

    def test_a_rearm_that_was_not_recorded_exits_nonzero(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        FakeSwitch.durable = False
        with pytest.raises(SystemExit) as exit_info:
            run(monkeypatch, "rearm", "--who", "operator", "--reason", "resume")
        assert exit_info.value.code == 1
        assert "remains engaged" in capsys.readouterr().out

    def test_a_recorded_kill_exits_zero_and_names_the_row(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        run(monkeypatch, "kill", "--who", "operator", "--reason", "halt")
        assert "risk_events.id=1" in capsys.readouterr().out

    def test_status_reports_the_current_state(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        FakeSwitch.engaged = True
        run(monkeypatch, "status")
        assert "HALTED" in capsys.readouterr().out
