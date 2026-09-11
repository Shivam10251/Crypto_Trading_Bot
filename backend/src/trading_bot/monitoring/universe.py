"""Which markets to monitor.

``explicit`` monitors the configured symbol lists. ``top_volume`` ranks every
pair listed on both spot and perpetual in the configured quote asset by the
*weaker* leg's 24h quote volume - a basis trade is bounded by the thinner of its
two markets - and monitors the top N, plus the explicit lists.

Checked against the live venue on 2026-09-11: 357 USDT pairs trade on both
sides. Perpetuals quoted in multiples of the spot coin (``1000PEPEUSDT`` against
``PEPEUSDT``) carry different symbols, so they are left out rather than paired
with a price a thousand times off.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal

from trading_bot.core.config import MarketsConfig, UniverseConfig
from trading_bot.core.logging import get_logger
from trading_bot.db.models.enums import MarketType
from trading_bot.exchange.base import ExchangeAdapter
from trading_bot.exchange.errors import UnknownMarketError
from trading_bot.exchange.models import MarketRef, MarketSpec

logger = get_logger(__name__)

_CLASSES = (MarketType.SPOT, MarketType.PERPETUAL)
Listings = dict[MarketType, dict[str, MarketSpec]]


@dataclass(frozen=True, slots=True)
class RankedPair:
    """A spot/perpetual pair that qualified for the ranking."""

    symbol: str
    spot: MarketSpec
    perpetual: MarketSpec
    spot_quote_volume: Decimal
    perpetual_quote_volume: Decimal

    @property
    def ranking_volume(self) -> Decimal:
        return min(self.spot_quote_volume, self.perpetual_quote_volume)


@dataclass(frozen=True, slots=True)
class Universe:
    """The markets to stream, in rank order, and how they were chosen."""

    specs: tuple[MarketSpec, ...]
    selection: str = "explicit"
    # Pair position (1 = most liquid) for ranked markets; both legs share it.
    # Explicitly configured markets that did not rank are absent.
    ranks: Mapping[MarketRef, int] = field(default_factory=dict)
    # Pairs listed on both spot and perpetual in the quote asset.
    candidates: int = 0
    excluded: Mapping[str, int] = field(default_factory=dict)

    @property
    def refs(self) -> tuple[MarketRef, ...]:
        return tuple(spec.ref for spec in self.specs)

    def describe(self) -> str:
        if self.selection == "explicit":
            count = len(self.specs)
            return f"{count} configured market{'' if count == 1 else 's'}"
        pairs = len(set(self.ranks.values()))
        text = f"top {pairs} of {self.candidates} spot/perpetual pairs by weaker-leg 24h volume"
        extra = len(self.specs) - len(self.ranks)
        return f"{text} + {extra} configured" if extra else text


async def select_universe(adapter: ExchangeAdapter, config: MarketsConfig) -> Universe:
    explicit = [(MarketType.SPOT, symbol) for symbol in config.spot_symbols] + [
        (MarketType.PERPETUAL, symbol) for symbol in config.perpetual_symbols
    ]
    ranking = config.selection == "top_volume"
    needed = set(_CLASSES) if ranking else {kind for kind, _ in explicit}
    listed: Listings = {}
    for kind in _CLASSES:
        if kind in needed:
            listed[kind] = {spec.symbol: spec for spec in await adapter.get_markets(kind)}

    specs: list[MarketSpec] = []
    ranks: dict[MarketRef, int] = {}
    candidates = 0
    excluded: Counter[str] = Counter()
    if ranking:
        pairs, candidates, excluded = await _rank_pairs(adapter, config.top_volume, listed)
        for position, pair in enumerate(pairs[: config.top_volume.count], start=1):
            for spec in (pair.spot, pair.perpetual):
                specs.append(spec)
                ranks[spec.ref] = position
    specs.extend(
        spec for spec in _resolve_explicit(explicit, listed, adapter.venue) if spec.ref not in ranks
    )
    if not specs:
        raise UnknownMarketError("no markets selected; check the markets configuration")

    universe = Universe(
        specs=tuple(specs),
        selection=config.selection,
        ranks=ranks,
        candidates=candidates,
        excluded=dict(excluded),
    )
    logger.info(
        "universe.selected",
        markets=len(specs),
        description=universe.describe(),
        excluded=dict(excluded),
    )
    return universe


async def _rank_pairs(
    adapter: ExchangeAdapter, rule: UniverseConfig, listed: Listings
) -> tuple[list[RankedPair], int, Counter[str]]:
    volumes: dict[MarketType, dict[str, Decimal]] = {}
    for kind in _CLASSES:
        stats = await adapter.get_daily_stats(kind)
        volumes[kind] = {entry.ref.symbol: entry.quote_volume for entry in stats}
    minimum = Decimal(str(rule.min_quote_volume))
    excluded_bases = set(rule.exclude_base_assets)
    excluded_symbols = set(rule.exclude_symbols)
    perpetuals = listed[MarketType.PERPETUAL]

    pairs: list[RankedPair] = []
    excluded: Counter[str] = Counter()
    candidates = 0
    for symbol, spot in listed[MarketType.SPOT].items():
        if spot.quote_asset != rule.quote_asset:
            continue
        perpetual = perpetuals.get(symbol)
        if perpetual is None or perpetual.quote_asset != rule.quote_asset:
            excluded["no perpetual"] += 1
            continue
        candidates += 1
        if not (spot.is_active and perpetual.is_active):
            excluded["not trading"] += 1
            continue
        if spot.base_asset in excluded_bases or symbol in excluded_symbols:
            excluded["excluded by configuration"] += 1
            continue
        pair = RankedPair(
            symbol=symbol,
            spot=spot,
            perpetual=perpetual,
            spot_quote_volume=volumes[MarketType.SPOT].get(symbol, Decimal(0)),
            perpetual_quote_volume=volumes[MarketType.PERPETUAL].get(symbol, Decimal(0)),
        )
        if pair.ranking_volume < minimum:
            excluded["below minimum volume"] += 1
            continue
        pairs.append(pair)
    pairs.sort(key=lambda pair: (-pair.ranking_volume, pair.symbol))
    return pairs, candidates, excluded


def _resolve_explicit(
    wanted: Sequence[tuple[MarketType, str]], listed: Listings, venue: str
) -> list[MarketSpec]:
    """Configured symbols must exist and trade, or nothing starts.

    Streaming the rest would leave a strategy blind to a leg it expects.
    """
    specs: list[MarketSpec] = []
    missing: list[str] = []
    halted: list[str] = []
    for kind, symbol in dict.fromkeys(wanted):
        spec = listed[kind].get(symbol)
        label = f"{symbol} {kind.value.lower()}"
        if spec is None:
            missing.append(label)
        elif not spec.is_active:
            halted.append(label)
        else:
            specs.append(spec)
    if missing:
        raise UnknownMarketError(f"not listed on {venue}: {', '.join(missing)}")
    if halted:
        raise UnknownMarketError(f"not trading on {venue}: {', '.join(halted)}")
    return specs
