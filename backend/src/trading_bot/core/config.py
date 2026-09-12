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
    # WebSocket hosts only; the adapter chooses the path per stream kind.
    spot_ws_url: str = "wss://stream.binance.com:9443"
    futures_rest_url: str = "https://fapi.binance.com"
    futures_ws_url: str = "wss://fstream.binance.com"
    request_timeout_seconds: int = Field(default=10, ge=1)
    max_reconnect_backoff_seconds: int = Field(default=30, ge=1)
    api_key: SecretStr = SecretStr("")
    api_secret: SecretStr = SecretStr("")

    @property
    def has_credentials(self) -> bool:
        return bool(self.api_key.get_secret_value() and self.api_secret.get_secret_value())


_STABLECOINS = (
    "USDC",
    "FDUSD",
    "TUSD",
    "USDP",
    "DAI",
    "USDE",
    "PYUSD",
    "USD1",
    "EUR",
    "AEUR",
    "EURI",
)


class UniverseConfig(ConfigSection):
    """The rule behind ``markets.selection: top_volume``."""

    count: int = Field(default=50, ge=1, le=500)
    quote_asset: str = "USDT"
    # Pairs rank by the weaker leg's 24h quote volume, and both legs must clear
    # this floor: a basis trade is bounded by the thinner of its two markets.
    min_quote_volume: float = Field(default=1_000_000, ge=0)
    # Pegged assets have no basis worth trading.
    exclude_base_assets: list[str] = Field(default_factory=lambda: list(_STABLECOINS))
    exclude_symbols: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _normalize(self) -> UniverseConfig:
        object.__setattr__(self, "quote_asset", self.quote_asset.upper())
        object.__setattr__(
            self, "exclude_base_assets", [a.upper() for a in self.exclude_base_assets]
        )
        object.__setattr__(self, "exclude_symbols", [s.upper() for s in self.exclude_symbols])
        return self


class MarketsConfig(ConfigSection):
    # "explicit" monitors exactly the symbol lists below. "top_volume" ranks
    # pairs listed on both spot and perpetual and monitors the top N - plus
    # the lists below, which are monitored in either mode.
    selection: Literal["explicit", "top_volume"] = "explicit"
    top_volume: UniverseConfig = Field(default_factory=UniverseConfig)
    spot_symbols: list[str] = Field(default_factory=lambda: ["BTCUSDT"])
    perpetual_symbols: list[str] = Field(default_factory=lambda: ["BTCUSDT"])

    @model_validator(mode="after")
    def _normalize(self) -> MarketsConfig:
        object.__setattr__(self, "spot_symbols", [s.upper() for s in self.spot_symbols])
        object.__setattr__(self, "perpetual_symbols", [s.upper() for s in self.perpetual_symbols])
        return self


class MarketDataConfig(ConfigSection):
    """Live market-data engine and service (Phase 3)."""

    include_depth: bool = True
    include_ticker: bool = True
    # Levels published per side, and the depth of the REST snapshot that seeds
    # each local book. The book is only trusted inside the price range the
    # snapshot covered - 100 spot BTC levels span roughly 12 USD - so the
    # snapshot must be far deeper than what is published.
    depth_levels: int = Field(default=20, ge=1, le=100)
    snapshot_depth: int = Field(default=1000, ge=5, le=1000)
    # A connection silent for this long makes every market it carries STALE.
    stale_after_ms: int = Field(default=2000, gt=0)
    # A market with no message of its own for this long is STALE even on a busy
    # connection. Binance pushes only changes, so a quiet market is unchanged,
    # not stale; this catches a single stream dying. Measured live: mid-cap
    # spot markets routinely go several seconds without a message.
    market_silence_ms: int = Field(default=30_000, gt=0)
    stale_check_interval_ms: int = Field(default=250, gt=0)
    # An open connection that delivers nothing for this long is treated as dead.
    idle_timeout_seconds: float = Field(default=10.0, gt=0)
    ping_interval_seconds: float = Field(default=20.0, gt=0)
    ping_timeout_seconds: float = Field(default=20.0, gt=0)
    reconnect_initial_backoff_seconds: float = Field(default=0.5, gt=0)
    # Floor between order-book rebuilds per market; each one costs REST weight.
    resync_min_interval_seconds: float = Field(default=1.0, ge=0)
    max_buffered_updates: int = Field(default=1000, ge=10)
    # Snapshots in flight at once. A 1000-level spot snapshot costs 50 of the
    # 6000 request weight Binance allows per minute; unthrottled, 50 markets
    # would spend 2500 of it in a single burst.
    max_concurrent_snapshots: int = Field(default=4, ge=1, le=20)
    # Liquidity: resting value within this distance of the mid, measured from
    # every level the local book knows rather than only the published ones.
    liquidity_band_bps: float = Field(default=10.0, gt=0, le=1000)
    # Order size, in quote currency, used to quote market-order slippage.
    reference_order_notional: float = Field(default=10_000.0, gt=0)
    # Sampled persistence into market_data: one row per market per interval,
    # written only when the quote changed. A row costs about 290 bytes with its
    # indexes, so 100 markets at 5 s is roughly 500 MB a day before retention.
    persist: bool = True
    persist_interval_ms: int = Field(default=5000, ge=100)
    # The API calls a market live when its newest stored quote is this recent.
    status_fresh_within_ms: int = Field(default=15000, gt=0)
    display: bool = True
    display_interval_ms: int = Field(default=1000, ge=100)

    @model_validator(mode="after")
    def _check_consistency(self) -> MarketDataConfig:
        if self.snapshot_depth < self.depth_levels:
            raise ValueError("snapshot_depth must be at least depth_levels")
        if self.market_silence_ms < self.stale_after_ms:
            raise ValueError("market_silence_ms must be at least stale_after_ms")
        if self.status_fresh_within_ms <= self.persist_interval_ms:
            # Otherwise every market would read stale between two samples.
            raise ValueError("status_fresh_within_ms must exceed persist_interval_ms")
        return self


class MonitoringConfig(ConfigSection):
    """Market monitoring (Phase 4): statistics over the live snapshots."""

    sample_interval_ms: int = Field(default=1000, ge=100)
    # Rolling window for spread and imbalance means and latency percentiles.
    window_seconds: int = Field(default=60, ge=5, le=3600)


class SpotPerpBasisConfig(ConfigSection):
    """The spot/perpetual basis strategy (Phase 5)."""

    # Net edge, after every cost, required before a signal is generated. The
    # threshold is on NET: gross spread is never traded on.
    min_net_edge_bps: float = Field(default=1.0, ge=0)
    # Size one opportunity is evaluated at, capped again by what the thinner
    # leg's book can absorb. The risk engine re-checks this in Phase 9.
    max_notional_usd: float = Field(default=1000.0, gt=0)
    # Selling the spot leg needs inventory or a margin borrow. On a plain spot
    # account only "buy spot, sell perp" is reachable, so the other direction
    # is detected and priced - it is research data - but never signalled.
    allow_spot_short: bool = False
    # Both legs must be this fresh, and no slower than this, or the basis
    # describes a market that has moved on. Freshness is judged per *input*,
    # not per market: a 24h ticker arriving keeps a market's aggregate age
    # small while the quote and the book the strategy actually prices against
    # go on ageing, so each has its own limit.
    max_data_age_ms: int = Field(default=2000, gt=0)
    max_quote_age_ms: int = Field(default=2000, gt=0)
    max_book_age_ms: int = Field(default=2000, gt=0)
    # Funding is polled over REST on a slow cadence and a failed poll keeps the
    # last known rates, so an observation can be minutes old. Beyond this it is
    # refused rather than retained indefinitely. Generous against the 60 s poll
    # because funding genuinely moves slowly - but finite.
    max_funding_age_ms: int = Field(default=600_000, gt=0)
    max_latency_ms: int = Field(default=500, gt=0)
    # A signal acted on after this point is stale by definition.
    signal_ttl_ms: int = Field(default=500, gt=0)
    # Funding rates move on the venue's schedule, not ours; polling the bulk
    # endpoint costs weight 10 however many markets are monitored.
    funding_refresh_seconds: float = Field(default=60.0, gt=0)


class StrategyConfig(ConfigSection):
    enabled: list[str] = Field(default_factory=lambda: ["spot_perp_basis"])
    spot_perp_basis: SpotPerpBasisConfig = Field(default_factory=SpotPerpBasisConfig)
    # Evaluate the enabled strategies inside the market-data service and show
    # what they see. Detection only - nothing is executed before Phase 8.
    evaluate: bool = True
    display: bool = True
    evaluate_interval_ms: int = Field(default=1000, ge=100)


class OpportunitiesConfig(ConfigSection):
    """Persisting what the strategies detect (Phase 7)."""

    persist: bool = True
    # An opportunity is one episode - a contiguous run of the same discrepancy
    # in the same direction - not one row per evaluation. Measured live at 50
    # pairs: per-cycle rows would be 4.0M a day, episodes are 81K.
    flush_interval_ms: int = Field(default=5000, ge=100)
    # Drop episodes shorter than this. Zero records everything, which is the
    # honest default: filtering by duration biases the dataset toward exactly
    # the long-lived opportunities the research is trying to count.
    min_duration_ms: int = Field(default=0, ge=0)


class CostsConfig(ConfigSection):
    """The venue's fee schedule and what the cost model assumes about execution.

    Defaults are the published binance.com VIP 0 rates. They are per account,
    not per venue, and Binance only exposes the real ones behind an
    authenticated endpoint - so they are configuration, and a venue that does
    report them overrides these.

    Note that spot maker and taker are the same rate: resting a limit order on
    the spot leg saves nothing. Only the perpetual leg rewards patience.
    """

    spot_maker_fee_bps: float = Field(default=10.0, ge=0)
    spot_taker_fee_bps: float = Field(default=10.0, ge=0)
    perp_maker_fee_bps: float = Field(default=2.0, ge=0)
    perp_taker_fee_bps: float = Field(default=5.0, ge=0)

    # Paying fees in BNB discounts them, by different amounts on each leg.
    pay_fees_in_bnb: bool = False
    bnb_discount_spot_pct: float = Field(default=25.0, ge=0, le=100)
    bnb_discount_futures_pct: float = Field(default=10.0, ge=0, le=100)

    # How the legs are assumed to fill. "maker" charges the cheaper rate and
    # assumes a resting order was hit - which Phase 8 has to earn, not assume.
    # Taker on both sides is the honest default: it is what the strategy's
    # book-walking prices already describe.
    entry_role: Literal["maker", "taker"] = "taker"
    exit_role: Literal["maker", "taker"] = "taker"

    # Extra margin subtracted from every edge so gross spread is never traded on.
    safety_buffer_bps: float = Field(default=2.0, ge=0)

    # How long the perpetual leg is assumed to be held, for charging funding.
    # Funding settles at fixed times, so this decides how many settlements a
    # position actually crosses - not a fraction of one.
    funding_horizon_minutes: float = Field(default=60.0, gt=0)

    # The basis the position is assumed to be closed at. The gross edge is a
    # *theoretical convergence* edge - what the trade is worth if the two mids
    # meet - and this says how far they are assumed to meet. Zero keeps the
    # assumption at full convergence, which is what the strategy has always
    # assumed; naming it makes the assumption auditable instead of implicit.
    # It is never tuned to make recorded results look better.
    assumed_terminal_basis_bps: float = Field(default=0.0, ge=0)
    # Account-specific spot margin borrow cost.  ``None`` means unknown and a
    # spot-short opportunity is unpriceable rather than free to borrow.
    spot_borrow_rate_bps_per_day: float | None = Field(default=None, ge=0)
    spot_borrow_rounding_hours: int = Field(default=1, ge=1, le=24)


class RiskConfig(ConfigSection):
    max_order_notional_usd: float = Field(default=1000.0, gt=0)
    max_position_notional_usd: float = Field(default=5000.0, gt=0)
    max_total_exposure_usd: float = Field(default=10000.0, gt=0)
    max_daily_loss_usd: float = Field(default=200.0, gt=0)
    max_consecutive_losses: int = Field(default=5, ge=1)
    # Aggregate across the attempt's legs, counting only adverse slippage -
    # the same definition before and after execution, so a pre-trade estimate
    # and a realised measurement are comparable. See docs/risk-management.md.
    max_slippage_bps: float = Field(default=15.0, gt=0)
    max_latency_ms: int = Field(default=500, gt=0)
    max_stale_data_ms: int = Field(default=2000, gt=0)
    # Funding arrives on a deliberately slower REST cadence than quotes and
    # books. Giving it the market-data limit would reject valid signals for
    # almost the entire interval between funding polls.
    max_funding_age_ms: int = Field(default=600_000, gt=0)

    # How often a running service re-reads durable kill-switch state, which
    # bounds how long a kill written by another process (the CLI, a second
    # service) takes to stop this one.
    kill_switch_poll_ms: int = Field(default=1000, ge=100, le=60_000)

    # Phase 10 does not exist yet, so there is no trustworthy realised P&L to
    # gate on. "deferred" reports the limit as unavailable and does not gate;
    # "fail_closed" refuses every signal while no P&L source is wired in,
    # rather than ever evaluating the limit against a fabricated zero.
    daily_loss_policy: Literal["deferred", "fail_closed"] = "deferred"
    consecutive_loss_policy: Literal["deferred", "fail_closed"] = "deferred"
    # How long a daily-loss breach halts trading for. It expires on its own
    # terms - "disabled for the day" - unlike a consecutive-loss breach,
    # which is a durable pause needing review and an explicit re-arm.
    daily_loss_halt_minutes: int = Field(default=1440, gt=0)

    # Whether a naked leg left behind by a real (non-shadow) attempt pauses
    # further entries until explicitly re-armed. Measured in Phase 8: limit
    # entries left 3 of 21 attempts unhedged, so this is not a rare case.
    pause_on_unhedged: bool = True
    # Whether realised slippage or latency past their limits, or an adapter
    # failure/timeout, pauses further entries. All three mean execution is
    # behaving differently from what was approved, which is the case the
    # architecture's "fail safe" principle exists for. Cross-leg fill skew is
    # deliberately *not* in this set - see docs/risk-management.md.
    pause_on_abnormal_execution: bool = True

    @model_validator(mode="after")
    def _check_hierarchy(self) -> RiskConfig:
        if self.max_order_notional_usd > self.max_position_notional_usd:
            raise ValueError("max_order_notional_usd cannot exceed max_position_notional_usd")
        if self.max_position_notional_usd > self.max_total_exposure_usd:
            raise ValueError("max_position_notional_usd cannot exceed max_total_exposure_usd")
        return self


class ExitPolicyConfig(ConfigSection):
    """When an open basis attempt is closed, and how the close is priced.

    Every condition here is a *reason to stop holding*, never a prediction
    that closing now is profitable. The price an exit gets comes from walking
    the current synchronised books for the exact residual quantity; nothing in
    this section can make a close look better than the book it filled against.
    """

    # Off by default, like execution itself: a service that closes positions
    # must be switched on deliberately.
    enabled: bool = False
    evaluate_interval_ms: int = Field(default=1000, ge=100)
    # Close once the basis has converged to within this many bps of flat,
    # measured in the direction the position was entered. Zero means full
    # convergence, matching ``costs.assumed_terminal_basis_bps``'s default.
    target_basis_bps: float = Field(default=0.0, ge=0)
    # Close regardless of the basis after this long. A basis position held
    # indefinitely is an unhedged bet on funding, not the trade that was made.
    max_holding_minutes: float = Field(default=240.0, gt=0)
    # Close when the basis has widened against the entry by this much. A stop,
    # not a target: it does not claim the exit is profitable.
    adverse_basis_bps: float = Field(default=25.0, gt=0)
    # A book older than this cannot price an exit, exactly as it cannot price
    # an entry (``execution.max_book_age_ms``).
    max_book_age_ms: int = Field(default=2000, gt=0)
    # How long a claimed-but-unfinished close may sit before another pass may
    # reconcile and retry it. Bounds recovery after a crash mid-exit.
    claim_timeout_ms: int = Field(default=30_000, gt=0)
    # Retries of a close that filled nothing. A close that keeps failing is an
    # operator's problem, not something to retry forever.
    max_close_attempts: int = Field(default=5, ge=1, le=100)


class PortfolioConfig(ConfigSection):
    """Portfolio valuation, P&L snapshots and the exit policy (Phase 10)."""

    # Off by default. With it off nothing closes positions and no snapshot is
    # written; the risk engine's P&L source then reports unavailable, which is
    # the honest state rather than a fabricated zero.
    enabled: bool = False
    # Snapshot cadence. Also the return-sampling interval for Sharpe/Sortino:
    # they are annualised from this, and from nothing else.
    snapshot_interval_ms: int = Field(default=60_000, ge=1000)
    # A mark older than this cannot value a position. The snapshot then says
    # DEGRADED (or UNAVAILABLE) rather than reusing the last price it saw.
    mark_max_age_ms: int = Field(default=5000, gt=0)
    # Sharpe and Sortino need enough regularly spaced observations to mean
    # anything. Below this they are stored as NULL, never estimated.
    min_return_observations: int = Field(default=30, ge=2)
    # Annual risk-free rate used for both ratios, in percent. Zero is the
    # honest default for a market-neutral crypto book quoted in USDT.
    risk_free_rate_annual_pct: float = Field(default=0.0, ge=0)
    # How fresh the risk engine's P&L view may be before it is re-read from
    # durable rows. Bounds the staleness of the daily-loss gate.
    pnl_refresh_ms: int = Field(default=1000, ge=100, le=60_000)
    exits: ExitPolicyConfig = Field(default_factory=ExitPolicyConfig)


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
    """Paper execution (Phase 8) and the guards around the live path (Phase 17)."""

    mode: ExecutionMode = ExecutionMode.PAPER
    live_enabled: bool = False
    live_confirmation_phrase: str = ""

    # Whether a validated signal is actually simulated. Off by default: the
    # strategy runs and records without anything pretending to trade.
    enabled: bool = False

    # Decision to venue. The order is priced against the book as it is AFTER
    # this delay, so the market gets to move first - which is the point.
    # Measured quote latency was 41-75 ms one way; 100 ms is a round trip plus
    # our own processing, and it is an assumption, not a measurement.
    latency_ms: int = Field(default=100, ge=0)
    latency_jitter_ms: int = Field(default=25, ge=0)
    latency_ms_by_market_type: dict[str, int] = Field(default_factory=dict)
    # An order that has not reached a terminal state within this fails.
    timeout_ms: int = Field(default=5000, gt=0)
    # A book older than this cannot price a fill; the order is refused rather
    # than filled against a price that may no longer exist.
    max_book_age_ms: int = Field(default=2000, gt=0)

    # How the entry is placed. "market" crosses the spread and always pays
    # taker; "limit" is IOC at the strategy's rounded price. GTC is not
    # simulated until trade prints and queue position are available.
    entry_order_type: Literal["market", "limit"] = "market"

    # Execution is decoupled from strategy evaluation by a bounded queue.
    # Overflow is rejected explicitly rather than allowed to create stale work.
    queue_size: int = Field(default=128, ge=1, le=10_000)
    workers: int = Field(default=2, ge=1, le=32)
    recent_attempts: int = Field(default=200, ge=2, le=10_000)
    max_cached_orders: int = Field(default=2_000, ge=2, le=100_000)
    max_leg_skew_ms: int = Field(default=250, gt=0)
    # A BNB fee discount is executable only when the paper account actually
    # owns BNB.  Zero is the safe default.
    paper_bnb_balance: float = Field(default=0.0, ge=0)
    paper_bnb_price_usd: float | None = Field(default=None, gt=0)
    paper_cash_usd: float = Field(default=100_000.0, gt=0)
    paper_spot_inventory: dict[str, float] = Field(default_factory=dict)
    paper_allow_margin_borrow: bool = False
    paper_max_borrow_usd: float = Field(default=0.0, ge=0)
    paper_perp_leverage: float = Field(default=1.0, ge=1, le=125)

    # Simulate the best *rejected* opportunity each cycle, to measure what a
    # round trip really costs. Measured live, no opportunity has ever passed
    # validation on this account, so without this the simulator would never
    # execute anything and its fill, partial-fill and expiry paths would be
    # exercised by unit tests alone.
    #
    # A shadow order is a PROBE, not a trade the strategy asked for. It is
    # flagged on the row (`orders.is_shadow`) and must be excluded from any
    # question about what the strategy would have earned.
    shadow: bool = False
    # Probes are spaced out: one per interval, not one per evaluation cycle.
    shadow_interval_ms: int = Field(default=60_000, gt=0)

    @model_validator(mode="after")
    def _check_timing(self) -> ExecutionConfig:
        normalized = {key.upper(): value for key, value in self.latency_ms_by_market_type.items()}
        valid = {"SPOT", "PERPETUAL", "FUTURE"}
        if normalized.keys() - valid or any(value < 0 for value in normalized.values()):
            raise ValueError("latency_ms_by_market_type needs non-negative known market types")
        object.__setattr__(self, "latency_ms_by_market_type", normalized)
        largest = max([self.latency_ms, *normalized.values()]) + self.latency_jitter_ms
        if self.timeout_ms <= largest:
            raise ValueError("timeout_ms must exceed the maximum configured latency plus jitter")
        return self


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
    market_data: MarketDataConfig = Field(default_factory=MarketDataConfig)
    monitoring: MonitoringConfig = Field(default_factory=MonitoringConfig)
    strategy: StrategyConfig = Field(default_factory=StrategyConfig)
    opportunities: OpportunitiesConfig = Field(default_factory=OpportunitiesConfig)
    costs: CostsConfig = Field(default_factory=CostsConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    portfolio: PortfolioConfig = Field(default_factory=PortfolioConfig)
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
        if (
            (self.execution.enabled or self.execution.shadow)
            and self.execution.mode is ExecutionMode.PAPER
            and self.costs.pay_fees_in_bnb
            and self.execution.paper_bnb_balance <= 0
        ):
            raise ValueError(
                "paper execution with BNB fee discounts requires paper_bnb_balance > 0"
            )
        if (
            (self.execution.enabled or self.execution.shadow)
            and self.execution.mode is ExecutionMode.PAPER
            and self.costs.pay_fees_in_bnb
            and self.execution.paper_bnb_price_usd is None
        ):
            raise ValueError("paper execution with BNB fee discounts requires paper_bnb_price_usd")
        if (
            (self.execution.enabled or self.execution.shadow)
            and self.execution.mode is ExecutionMode.PAPER
            and self.costs.pay_fees_in_bnb
            and self.execution.paper_bnb_price_usd is not None
        ):
            available = self.execution.paper_bnb_balance * self.execution.paper_bnb_price_usd
            max_rate = max(self.costs.spot_taker_fee_bps, self.costs.perp_taker_fee_bps)
            required = self.risk.max_total_exposure_usd * max_rate / 10_000
            if available < required:
                raise ValueError(
                    "paper BNB balance is too small for maximum configured exposure fees"
                )
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached settings for the process. Call ``get_settings.cache_clear()`` in tests."""
    return Settings()
