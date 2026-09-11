"""Configuration loading and validation.

Layering, lowest precedence first:

1. ``config/base.yaml``          - defaults shared by all profiles
2. ``config/<profile>.yaml``     - per-profile overrides
3. ``TB_*`` environment variables / ``.env`` - machine-specific values & secrets

Secrets (database password, exchange keys) live only in the environment.
The settings object is immutable once built and cached per process.
"""

from __future__ import annotations

import os
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

# repo_root/backend/src/trading_bot/core/config.py -> repo_root
REPO_ROOT = Path(__file__).resolve().parents[4]
CONFIG_DIR = REPO_ROOT / "config"
ENV_FILE = REPO_ROOT / ".env"


class Profile(StrEnum):
    DEVELOPMENT = "development"
    PAPER = "paper"
    PRODUCTION = "production"


class ExecutionMode(StrEnum):
    PAPER = "paper"
    LIVE = "live"


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge ``override`` into ``base`` without mutating either."""
    merged = dict(base)
    for key, value in override.items():
        current = merged.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            merged[key] = _deep_merge(current, value)
        else:
            merged[key] = value
    return merged


def active_profile() -> Profile:
    """Profile named by ``TB_PROFILE``, defaulting to development."""
    raw = os.getenv("TB_PROFILE", Profile.DEVELOPMENT.value).strip().lower()
    try:
        return Profile(raw)
    except ValueError as exc:  # pragma: no cover - guarded by test_config
        valid = ", ".join(p.value for p in Profile)
        raise ValueError(f"TB_PROFILE={raw!r} is not one of: {valid}") from exc


def load_yaml_config(profile: Profile, config_dir: Path | None = None) -> dict[str, Any]:
    """Merge ``base.yaml`` with the profile's YAML file."""
    directory = config_dir or CONFIG_DIR
    base_path = directory / "base.yaml"
    if not base_path.is_file():
        raise FileNotFoundError(f"missing base configuration: {base_path}")

    def read(path: Path) -> dict[str, Any]:
        if not path.is_file():
            return {}
        content = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(content, dict):
            raise ValueError(f"{path} must contain a YAML mapping")
        return content

    merged = _deep_merge(read(base_path), read(directory / f"{profile.value}.yaml"))
    merged.setdefault("app", {})["profile"] = profile.value
    return merged


class ConfigSection(BaseModel):
    """Base for every settings section.

    Frozen so a resolved configuration cannot drift at runtime: risk limits and
    execution flags must mean the same thing for the whole life of the process.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")


class AppConfig(ConfigSection):
    name: str = "Arbitrage Terminal"
    profile: Profile = Profile.DEVELOPMENT


class ApiConfig(ConfigSection):
    host: str = "127.0.0.1"
    port: int = Field(default=8000, ge=1, le=65535)
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:5173"])
    docs_enabled: bool = True


class LoggingConfig(ConfigSection):
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    format: Literal["console", "json"] = "console"
    audit_events: list[str] = Field(default_factory=list)


class DatabaseConfig(ConfigSection):
    host: str = "127.0.0.1"
    port: int = Field(default=5432, ge=1, le=65535)
    name: str = "trading_bot"
    user: str = "trading_bot"
    password: SecretStr = SecretStr("")
    pool_size: int = Field(default=10, ge=1)
    max_overflow: int = Field(default=5, ge=0)
    pool_timeout_seconds: int = Field(default=30, ge=1)
    echo_sql: bool = False
    # Set by tests / tooling to bypass the PostgreSQL DSN entirely.
    url_override: str | None = None

    def dsn(self, *, async_driver: bool = True) -> str:
        """SQLAlchemy URL. Password is only interpolated here, never logged."""
        if self.url_override:
            return self.url_override
        driver = "postgresql+asyncpg" if async_driver else "postgresql+psycopg"
        secret = self.password.get_secret_value()
        credentials = f"{self.user}:{secret}" if secret else self.user
        return f"{driver}://{credentials}@{self.host}:{self.port}/{self.name}"

    def safe_dsn(self) -> str:
        """DSN with the password masked - safe for logs and health payloads."""
        if self.url_override:
            return self.url_override
        return f"postgresql+asyncpg://{self.user}:***@{self.host}:{self.port}/{self.name}"


class ExchangeConfig(ConfigSection):
    venue: str = "binance"
    spot_rest_url: str = "https://api.binance.com"
    spot_ws_url: str = "wss://stream.binance.com:9443/ws"
    futures_rest_url: str = "https://fapi.binance.com"
    futures_ws_url: str = "wss://fstream.binance.com/ws"
    request_timeout_seconds: int = Field(default=10, ge=1)
    max_reconnect_backoff_seconds: int = Field(default=30, ge=1)
    api_key: SecretStr = SecretStr("")
    api_secret: SecretStr = SecretStr("")

    @property
    def has_credentials(self) -> bool:
        return bool(self.api_key.get_secret_value() and self.api_secret.get_secret_value())


class MarketsConfig(ConfigSection):
    spot_symbols: list[str] = Field(default_factory=lambda: ["BTCUSDT"])
    perpetual_symbols: list[str] = Field(default_factory=lambda: ["BTCUSDT"])

    @model_validator(mode="after")
    def _normalize(self) -> MarketsConfig:
        object.__setattr__(self, "spot_symbols", [s.upper() for s in self.spot_symbols])
        object.__setattr__(self, "perpetual_symbols", [s.upper() for s in self.perpetual_symbols])
        return self


class StrategyConfig(ConfigSection):
    enabled: list[str] = Field(default_factory=lambda: ["spot_perp_basis"])


class CostsConfig(ConfigSection):
    spot_taker_fee_bps: float = Field(default=10.0, ge=0)
    perp_taker_fee_bps: float = Field(default=5.0, ge=0)
    safety_buffer_bps: float = Field(default=2.0, ge=0)


class RiskConfig(ConfigSection):
    max_order_notional_usd: float = Field(default=1000.0, gt=0)
    max_position_notional_usd: float = Field(default=5000.0, gt=0)
    max_total_exposure_usd: float = Field(default=10000.0, gt=0)
    max_daily_loss_usd: float = Field(default=200.0, gt=0)
    max_consecutive_losses: int = Field(default=5, ge=1)
    max_slippage_bps: float = Field(default=15.0, gt=0)
    max_latency_ms: int = Field(default=500, gt=0)
    max_stale_data_ms: int = Field(default=2000, gt=0)

    @model_validator(mode="after")
    def _check_hierarchy(self) -> RiskConfig:
        if self.max_order_notional_usd > self.max_position_notional_usd:
            raise ValueError("max_order_notional_usd cannot exceed max_position_notional_usd")
        if self.max_position_notional_usd > self.max_total_exposure_usd:
            raise ValueError("max_position_notional_usd cannot exceed max_total_exposure_usd")
        return self


class RetentionConfig(ConfigSection):
    """How long high-frequency raw data is kept.

    Opportunities, orders, fills, positions, P&L and event rows are never
    purged - they are the research dataset and the audit trail. Only the
    high-volume raw feeds below have a finite life.
    """

    enabled: bool = True
    market_data_days: int = Field(default=7, ge=1)
    order_books_days: int = Field(default=3, ge=1)
    trades_market_days: int = Field(default=7, ge=1)
    # Rows deleted per statement, so a purge cannot lock a table for long.
    purge_batch_size: int = Field(default=10_000, ge=100)


class ExecutionConfig(ConfigSection):
    mode: ExecutionMode = ExecutionMode.PAPER
    live_enabled: bool = False
    live_confirmation_phrase: str = ""


class Settings(BaseSettings):
    """Fully resolved, immutable application settings."""

    model_config = SettingsConfigDict(
        env_prefix="TB_",
        env_nested_delimiter="__",
        env_file=ENV_FILE if ENV_FILE.is_file() else None,
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )

    app: AppConfig = Field(default_factory=AppConfig)
    api: ApiConfig = Field(default_factory=ApiConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    exchange: ExchangeConfig = Field(default_factory=ExchangeConfig)
    markets: MarketsConfig = Field(default_factory=MarketsConfig)
    strategy: StrategyConfig = Field(default_factory=StrategyConfig)
    costs: CostsConfig = Field(default_factory=CostsConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    retention: RetentionConfig = Field(default_factory=RetentionConfig)
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # Highest precedence first: explicit args, env, .env, then YAML defaults.
        def yaml_source() -> dict[str, Any]:
            return load_yaml_config(active_profile())

        return (init_settings, env_settings, dotenv_settings, yaml_source)  # type: ignore[return-value]

    @property
    def profile(self) -> Profile:
        return self.app.profile

    @property
    def is_live_execution_armed(self) -> bool:
        """True only when every live-trading precondition is satisfied.

        Phase 17 wires real order routing behind this flag. Until then nothing
        reads it except the guard below and its tests.
        """
        return (
            self.profile is Profile.PRODUCTION
            and self.execution.live_enabled
            and self.execution.mode is ExecutionMode.LIVE
            and bool(self.execution.live_confirmation_phrase.strip())
        )

    @model_validator(mode="after")
    def _enforce_live_trading_guards(self) -> Settings:
        """Live trading must be impossible outside an explicitly armed production run."""
        live_requested = self.execution.live_enabled or self.execution.mode is ExecutionMode.LIVE
        if live_requested and self.profile is not Profile.PRODUCTION:
            raise ValueError(
                f"live execution requested under profile {self.profile.value!r}; "
                "only the production profile may enable it"
            )
        if self.execution.live_enabled and not self.execution.live_confirmation_phrase.strip():
            raise ValueError(
                "live_enabled=true requires TB_EXECUTION__LIVE_CONFIRMATION_PHRASE to be set"
            )
        if self.is_live_execution_armed and not self.exchange.has_credentials:
            raise ValueError("live execution armed but exchange API credentials are missing")
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached settings for the process. Call ``get_settings.cache_clear()`` in tests."""
    return Settings()
