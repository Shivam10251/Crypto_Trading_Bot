"""Episodes: one row per discrepancy, not one per evaluation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from tests.unit.test_spot_perp_basis import PERP, SPOT, funding_info, view
from trading_bot.core.config import CostsConfig, SpotPerpBasisConfig
from trading_bot.exchange.models import MarketRef
from trading_bot.opportunities.episodes import EpisodeTracker
from trading_bot.strategy.base import StrategyContext
from trading_bot.strategy.basis import SpotPerpBasisStrategy
from trading_bot.strategy.costs import TransactionCostModel
from trading_bot.strategy.models import RejectionReason
from trading_bot.strategy.runner import StrategyRunner

NOW = datetime(2026, 9, 11, 16, 30, tzinfo=UTC)
FREE = CostsConfig(spot_taker_fee_bps=0.0, perp_taker_fee_bps=0.0, safety_buffer_bps=0.0)
# Perp rich -> buy spot, the direction a cash account can reach.
RICH = [view(SPOT, "99.99", "100.01").snapshot, view(PERP, "100.99", "101.01").snapshot]
# Perp cheap -> sell spot, the opposite direction.
CHEAP = [view(SPOT, "100.99", "101.01").snapshot, view(PERP, "99.99", "100.01").snapshot]
WIDER = [view(SPOT, "99.99", "100.01").snapshot, view(PERP, "101.99", "102.01").snapshot]


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


def make_runner(costs: CostsConfig = FREE) -> tuple[StrategyRunner, Clock]:
    clock = Clock()
    runner = StrategyRunner(
        [SpotPerpBasisStrategy(SpotPerpBasisConfig())],
        StrategyContext(cost_model=TransactionCostModel(costs)),
        clock=clock,
    )
    runner.set_funding({PERP: funding_info(interval=8)})
    return runner, clock


def test_repeated_evaluations_are_one_episode() -> None:
    """The core reduction: 8,378 live samples were 168 episodes."""
    runner, clock = make_runner()
    tracker = EpisodeTracker()
    for _ in range(10):
        assert tracker.update(runner.evaluate(RICH), clock.now) == []
        clock.advance(1)
    assert tracker.opened == 1
    assert len(tracker.open_episodes()) == 1
    assert tracker.open_episodes()[0].samples == 10


def test_an_episode_closes_when_the_direction_flips() -> None:
    runner, clock = make_runner()
    tracker = EpisodeTracker()
    for _ in range(4):  # observed at t=0..3
        tracker.update(runner.evaluate(RICH), clock.now)
        clock.advance(1)
    closed = tracker.update(runner.evaluate(CHEAP), clock.now)
    assert len(closed) == 1
    assert closed[0].duration_ms == 3000  # first observation to last
    # The opposite direction is a different trade, so a new episode opened.
    assert len(tracker.open_episodes()) == 1


def test_an_episode_closes_when_the_pair_stops_being_priceable() -> None:
    runner, clock = make_runner()
    tracker = EpisodeTracker()
    tracker.update(runner.evaluate(RICH), clock.now)
    clock.advance(2)
    tracker.update(runner.evaluate(RICH), clock.now)
    clock.advance(1)
    closed = tracker.update(runner.evaluate([]), clock.now)  # feed went away
    assert len(closed) == 1
    assert closed[0].duration_ms == 2000
    assert tracker.open_episodes() == []


def test_duration_is_a_lower_bound_measured_between_observations() -> None:
    """We cannot know when it started or ended between samples, so we understate.

    An episode seen exactly once is 0 ms: one instant, nothing more to claim.
    """
    runner, clock = make_runner()
    tracker = EpisodeTracker()
    tracker.update(runner.evaluate(RICH), clock.now)
    clock.advance(5)
    (closed,) = tracker.update(runner.evaluate([]), clock.now)
    assert closed.samples == 1
    assert closed.duration_ms == 0


def test_the_episode_keeps_its_best_moment_not_its_first() -> None:
    """If the peak never cleared costs, no moment did."""
    runner, clock = make_runner()
    tracker = EpisodeTracker()
    tracker.update(runner.evaluate(RICH), clock.now)  # 100 bps
    clock.advance(1)
    tracker.update(runner.evaluate(WIDER), clock.now)  # 200 bps
    clock.advance(1)
    tracker.update(runner.evaluate(RICH), clock.now)  # back to 100 bps
    (episode,) = tracker.open_episodes()
    assert episode.samples == 3
    assert episode.best_net_edge_bps is not None
    assert episode.best_net_edge_bps > 150  # the peak, not the first or last
    assert episode.best_at == NOW + timedelta(seconds=1)


def test_duration_spans_the_whole_episode_not_the_best_moment() -> None:
    runner, clock = make_runner()
    tracker = EpisodeTracker()
    tracker.update(runner.evaluate(WIDER), clock.now)  # best is first
    for _ in range(5):
        clock.advance(1)
        tracker.update(runner.evaluate(RICH), clock.now)
    (episode,) = tracker.open_episodes()
    assert episode.best_at == NOW
    assert episode.duration_ms == 5000


def test_every_rejection_reason_is_remembered() -> None:
    """A rejection that happened must not be erased by a later one."""
    runner, clock = make_runner(CostsConfig())
    tracker = EpisodeTracker()
    tracker.update(runner.evaluate(CHEAP), clock.now)
    (episode,) = tracker.open_episodes()
    assert RejectionReason.SPOT_SHORT_UNAVAILABLE in episode.rejections


def test_an_actionable_evaluation_marks_the_episode() -> None:
    runner, clock = make_runner()
    tracker = EpisodeTracker()
    tracker.update(runner.evaluate(RICH), clock.now)
    (episode,) = tracker.open_episodes()
    assert episode.ever_actionable


def test_a_priced_evaluation_beats_an_unpriced_one() -> None:
    """An episode that was ever priceable must be storable."""
    runner, clock = make_runner()
    runner.set_funding({})  # no funding -> unpriceable
    tracker = EpisodeTracker()
    tracker.update(runner.evaluate(RICH), clock.now)
    (episode,) = tracker.open_episodes()
    assert episode.best.edge is None
    clock.advance(1)
    runner.set_funding({PERP: funding_info(interval=8)})
    tracker.update(runner.evaluate(RICH), clock.now)
    (episode,) = tracker.open_episodes()
    assert episode.best.edge is not None


def test_close_all_ends_everything_for_shutdown() -> None:
    runner, clock = make_runner()
    tracker = EpisodeTracker()
    tracker.update(runner.evaluate(RICH), clock.now)
    assert len(tracker.close_all()) == 1
    assert tracker.open_episodes() == []
    assert tracker.closed == 1


def test_two_pairs_are_two_independent_episodes() -> None:
    eth_spot = MarketRef(SPOT.venue, "ETHUSDT", SPOT.market_type)
    eth_perp = MarketRef(PERP.venue, "ETHUSDT", PERP.market_type)
    runner, clock = make_runner()
    runner.set_funding({PERP: funding_info(interval=8), eth_perp: funding_info(interval=8)})
    tracker = EpisodeTracker()
    tracker.update(
        runner.evaluate(
            [
                *RICH,
                view(eth_spot, "99.99", "100.01").snapshot,
                view(eth_perp, "100.99", "101.01").snapshot,
            ]
        ),
        clock.now,
    )
    assert tracker.opened == 2
    clock.advance(1)
    # One pair goes away; the other must be untouched.
    closed = tracker.update(runner.evaluate(RICH), clock.now)
    assert len(closed) == 1
    assert closed[0].key[1].symbol == "ETHUSDT"
    assert len(tracker.open_episodes()) == 1
