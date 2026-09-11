"""Logging pipeline: structure, levels and credential redaction."""

from __future__ import annotations

import json

import pytest
import structlog

from trading_bot.core.config import LoggingConfig
from trading_bot.core.logging import configure_logging, get_logger


@pytest.fixture(autouse=True)
def _reset_structlog() -> None:
    structlog.reset_defaults()


def _capture() -> structlog.testing.LogCapture:
    capture = structlog.testing.LogCapture()
    structlog.configure(processors=[*_shared_processors(), capture])
    return capture


def _shared_processors() -> list[structlog.types.Processor]:
    from trading_bot.core.logging import _redact

    return [structlog.stdlib.add_log_level, _redact]


class TestRedaction:
    @pytest.mark.parametrize(
        "field",
        ["password", "api_key", "api_secret", "secret", "token", "authorization", "signature"],
    )
    def test_sensitive_fields_are_masked(self, field: str) -> None:
        capture = _capture()
        get_logger("t").info("event", **{field: "leak-me"})
        assert capture.entries[0][field] == "***"

    def test_redaction_is_case_insensitive(self) -> None:
        capture = _capture()
        get_logger("t").info("event", API_KEY="leak-me")
        assert capture.entries[0]["API_KEY"] == "***"

    def test_non_sensitive_fields_survive(self) -> None:
        capture = _capture()
        get_logger("t").info("event", symbol="BTCUSDT", edge_bps=3.2)
        entry = capture.entries[0]
        assert entry["symbol"] == "BTCUSDT"
        assert entry["edge_bps"] == 3.2


class TestConfiguration:
    def test_json_format_emits_parseable_lines(self, capsys: pytest.CaptureFixture[str]) -> None:
        configure_logging(LoggingConfig(level="INFO", format="json"), force=True)
        get_logger("test").info("order.submitted", symbol="BTCUSDT", api_key="leak-me")
        payload = json.loads(capsys.readouterr().err.strip().splitlines()[-1])
        assert payload["event"] == "order.submitted"
        assert payload["symbol"] == "BTCUSDT"
        assert payload["api_key"] == "***"
        assert payload["level"] == "info"
        assert payload["timestamp"].endswith("Z")

    def test_console_format_is_human_readable(self, capsys: pytest.CaptureFixture[str]) -> None:
        configure_logging(LoggingConfig(level="INFO", format="console"), force=True)
        get_logger("test").info("market.tick", symbol="BTCUSDT")
        err = capsys.readouterr().err
        assert "market.tick" in err
        assert "BTCUSDT" in err

    def test_level_filtering_drops_lower_levels(self, capsys: pytest.CaptureFixture[str]) -> None:
        configure_logging(LoggingConfig(level="WARNING", format="json"), force=True)
        logger = get_logger("test")
        logger.debug("should.not.appear")
        logger.info("should.not.appear.either")
        logger.warning("should.appear")
        err = capsys.readouterr().err
        assert "should.not.appear" not in err
        assert "should.appear" in err

    def test_configuration_is_idempotent_without_force(self) -> None:
        configure_logging(LoggingConfig(level="INFO", format="json"), force=True)
        before = structlog.get_config()["wrapper_class"]
        configure_logging(LoggingConfig(level="ERROR", format="console"))
        assert structlog.get_config()["wrapper_class"] is before


class TestStdlibIntegration:
    """Third-party loggers must not drown the application's own events."""

    def test_sqlalchemy_statements_are_suppressed_by_default(self) -> None:
        import logging

        configure_logging(LoggingConfig(level="DEBUG", format="json"), force=True)
        assert logging.getLogger("sqlalchemy.engine").level == logging.WARNING

    def test_uvicorn_follows_configured_level_but_never_below_info(self) -> None:
        import logging

        configure_logging(LoggingConfig(level="DEBUG", format="json"), force=True)
        assert logging.getLogger("uvicorn.access").level == logging.INFO

        configure_logging(LoggingConfig(level="ERROR", format="json"), force=True)
        assert logging.getLogger("uvicorn.access").level == logging.ERROR
