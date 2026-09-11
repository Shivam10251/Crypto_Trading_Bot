"""Configuration layering, validation and live-trading guards."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from trading_bot.core.config import (
    CONFIG_DIR,
    DatabaseConfig,
    ExecutionMode,
    MarketDataConfig,
    Profile,
    RiskConfig,
    Settings,
    active_profile,
    load_yaml_config,
)


class TestYamlLoading:
    def test_base_and_profile_are_merged(self) -> None:
        merged = load_yaml_config(Profile.DEVELOPMENT)
        # from base.yaml
        assert merged["exchange"]["venue"] == "binance"
        # overridden by development.yaml
        assert merged["logging"]["level"] == "DEBUG"
        assert merged["app"]["profile"] == "development"

    def test_every_shipped_profile_loads(self) -> None:
        for profile in Profile:
            assert load_yaml_config(profile)["app"]["profile"] == profile.value

    def test_profile_override_does_not_mutate_base(self) -> None:
        paper = load_yaml_config(Profile.PAPER)
        development = load_yaml_config(Profile.DEVELOPMENT)
        assert paper["logging"]["format"] == "json"
        assert development["logging"]["format"] == "console"

    def test_missing_base_config_is_an_error(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            load_yaml_config(Profile.DEVELOPMENT, config_dir=tmp_path)

    def test_config_dir_resolves_to_repo_config(self) -> None:
        assert (CONFIG_DIR / "base.yaml").is_file()


class TestProfileSelection:
    def test_defaults_to_development(self) -> None:
        assert active_profile() is Profile.DEVELOPMENT

    def test_reads_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TB_PROFILE", "paper")
        assert active_profile() is Profile.PAPER

    def test_rejects_unknown_profile(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TB_PROFILE", "staging")
        with pytest.raises(ValueError, match="not one of"):
            active_profile()


class TestPrecedence:
    def test_env_overrides_yaml(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TB_LOGGING__LEVEL", "ERROR")
        assert Settings().logging.level == "ERROR"

    def test_nested_env_delimiter(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TB_DATABASE__PORT", "6543")
        assert Settings().database.port == 6543

    def test_init_args_win_over_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TB_DATABASE__NAME", "from_env")
        settings = Settings(database=DatabaseConfig(name="from_arg"))
        assert settings.database.name == "from_arg"

    def test_settings_are_immutable(self) -> None:
        settings = Settings()
        with pytest.raises(ValidationError):
            settings.api.port = 9999  # type: ignore[misc]


class TestSecretHandling:
    def test_password_is_masked_in_repr(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TB_DATABASE__PASSWORD", "super-secret")
        settings = Settings()
        assert "super-secret" not in repr(settings)
        assert settings.database.password.get_secret_value() == "super-secret"

    def test_dsn_contains_password_but_safe_dsn_does_not(self) -> None:
        config = DatabaseConfig(password="pw123", user="u", host="h", port=1, name="n")  # type: ignore[arg-type]
        assert "pw123" in config.dsn()
        assert "pw123" not in config.safe_dsn()
        assert "***" in config.safe_dsn()

    def test_url_override_bypasses_dsn_construction(self) -> None:
        config = DatabaseConfig(url_override="sqlite+aiosqlite:///:memory:")
        assert config.dsn() == "sqlite+aiosqlite:///:memory:"

    def test_credentials_absent_by_default(self) -> None:
        assert Settings().exchange.has_credentials is False


class TestLiveTradingGuards:
    """Live execution must be structurally impossible unless fully armed."""

    def test_paper_is_the_default(self) -> None:
        settings = Settings()
        assert settings.execution.mode is ExecutionMode.PAPER
        assert settings.execution.live_enabled is False
        assert settings.is_live_execution_armed is False

    def test_development_profile_cannot_enable_live(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TB_EXECUTION__LIVE_ENABLED", "true")
        with pytest.raises(ValidationError, match="only the production profile"):
            Settings()

    def test_paper_profile_cannot_enable_live(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TB_PROFILE", "paper")
        monkeypatch.setenv("TB_EXECUTION__MODE", "live")
        with pytest.raises(ValidationError, match="only the production profile"):
            Settings()

    def test_production_live_requires_confirmation_phrase(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TB_PROFILE", "production")
        monkeypatch.setenv("TB_EXECUTION__LIVE_ENABLED", "true")
        with pytest.raises(ValidationError, match="CONFIRMATION_PHRASE"):
            Settings()

    def test_production_live_requires_api_credentials(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TB_PROFILE", "production")
        monkeypatch.setenv("TB_EXECUTION__LIVE_ENABLED", "true")
        monkeypatch.setenv("TB_EXECUTION__MODE", "live")
        monkeypatch.setenv("TB_EXECUTION__LIVE_CONFIRMATION_PHRASE", "I understand the risk")
        with pytest.raises(ValidationError, match="credentials are missing"):
            Settings()

    def test_fully_armed_production_is_the_only_live_path(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TB_PROFILE", "production")
        monkeypatch.setenv("TB_EXECUTION__LIVE_ENABLED", "true")
        monkeypatch.setenv("TB_EXECUTION__MODE", "live")
        monkeypatch.setenv("TB_EXECUTION__LIVE_CONFIRMATION_PHRASE", "I understand the risk")
        monkeypatch.setenv("TB_EXCHANGE__API_KEY", "key")
        monkeypatch.setenv("TB_EXCHANGE__API_SECRET", "secret")
        assert Settings().is_live_execution_armed is True

    def test_shipped_profiles_all_disable_live(self) -> None:
        for profile in Profile:
            raw = load_yaml_config(profile)
            assert raw["execution"]["live_enabled"] is False
            assert raw["execution"]["mode"] == "paper"


class TestRiskConfigValidation:
    def test_order_cannot_exceed_position_limit(self) -> None:
        with pytest.raises(ValidationError, match="max_order_notional_usd cannot exceed"):
            RiskConfig(max_order_notional_usd=10_000, max_position_notional_usd=1_000)

    def test_position_cannot_exceed_total_exposure(self) -> None:
        with pytest.raises(ValidationError, match="max_position_notional_usd cannot exceed"):
            RiskConfig(max_position_notional_usd=50_000, max_total_exposure_usd=10_000)

    def test_limits_must_be_positive(self) -> None:
        with pytest.raises(ValidationError):
            RiskConfig(max_daily_loss_usd=0)

    def test_shipped_defaults_are_consistent(self) -> None:
        risk = Settings().risk
        assert risk.max_order_notional_usd <= risk.max_position_notional_usd
        assert risk.max_position_notional_usd <= risk.max_total_exposure_usd


class TestMarketsConfig:
    def test_symbols_are_upper_cased(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TB_MARKETS__SPOT_SYMBOLS", '["btcusdt","ethusdt"]')
        assert Settings().markets.spot_symbols == ["BTCUSDT", "ETHUSDT"]

    def test_defaults_start_with_one_market(self) -> None:
        settings = Settings()
        assert settings.markets.spot_symbols == ["BTCUSDT"]
        assert settings.markets.perpetual_symbols == ["BTCUSDT"]

    def test_market_list_is_not_hard_coded_to_a_length(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        symbols = [f"SYM{i}USDT" for i in range(60)]
        monkeypatch.setenv("TB_MARKETS__SPOT_SYMBOLS", repr(symbols).replace("'", '"'))
        assert len(Settings().markets.spot_symbols) == 60


class TestMarketDataConfig:
    def test_snapshot_covers_more_than_is_published(self) -> None:
        config = Settings().market_data
        assert config.snapshot_depth > config.depth_levels
        assert config.include_depth
        assert config.include_ticker

    def test_snapshot_shallower_than_published_depth_rejected(self) -> None:
        with pytest.raises(ValidationError, match="snapshot_depth"):
            MarketDataConfig(depth_levels=50, snapshot_depth=20)

    def test_overridable_from_environment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TB_MARKET_DATA__STALE_AFTER_MS", "750")
        assert Settings().market_data.stale_after_ms == 750

    def test_websocket_urls_are_hosts_without_a_path(self) -> None:
        """Paths are chosen per stream kind; a baked-in /ws would break routing."""
        exchange = Settings().exchange
        for url in (exchange.spot_ws_url, exchange.futures_ws_url):
            assert url.startswith("wss://")
            assert url.count("/") == 2


class TestCachedSettings:
    def test_get_settings_is_cached(self) -> None:
        from trading_bot.core.config import get_settings

        assert get_settings() is get_settings()
