"""The research record: what the strategies detected, stored for later.

Every opportunity is kept, profitable or not, as an *episode* - one contiguous
period during which the same discrepancy held - rather than one row per
evaluation. Nothing here decides anything; it only records what was decided.
"""

from trading_bot.opportunities.episodes import (
    EpisodeKey,
    EpisodeTracker,
    OpportunityEpisode,
    episode_key,
)
from trading_bot.opportunities.recorder import (
    MAX_PENDING_EPISODES,
    OpportunityRecorder,
    opportunity_row,
    signal_rows,
)

__all__ = [
    "MAX_PENDING_EPISODES",
    "EpisodeKey",
    "EpisodeTracker",
    "OpportunityEpisode",
    "OpportunityRecorder",
    "episode_key",
    "opportunity_row",
    "signal_rows",
]
