"""Building the enabled strategies from configuration.

A strategy is named in ``strategy.enabled`` and constructed here. Keeping the
construction in one place means a strategy module never reads configuration
itself - it is handed exactly what it needs, which is what makes it testable
without a settings object.
"""

from __future__ import annotations

from collections.abc import Callable

from trading_bot.core.config import StrategyConfig
from trading_bot.strategy.base import Strategy
from trading_bot.strategy.basis import STRATEGY_NAME as SPOT_PERP_BASIS
from trading_bot.strategy.basis import SpotPerpBasisStrategy

Builder = Callable[[StrategyConfig], Strategy]

BUILDERS: dict[str, Builder] = {
    SPOT_PERP_BASIS: lambda config: SpotPerpBasisStrategy(config.spot_perp_basis),
}


class UnknownStrategyError(LookupError):
    """A configured strategy name has no implementation."""


def build_strategies(config: StrategyConfig) -> list[Strategy]:
    """Construct every enabled strategy, in configured order.

    An unknown name fails loudly at startup rather than silently trading with
    fewer strategies than the operator asked for.
    """
    strategies: list[Strategy] = []
    for name in config.enabled:
        builder = BUILDERS.get(name)
        if builder is None:
            known = ", ".join(sorted(BUILDERS)) or "none"
            raise UnknownStrategyError(f"unknown strategy {name!r}; available: {known}")
        strategies.append(builder(config))
    return strategies
