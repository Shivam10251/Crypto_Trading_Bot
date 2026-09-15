"""Temporal validation, pre-start initialization, and a complete dataset fingerprint."""

from __future__ import annotations

import contextlib
import dataclasses
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import pytest

from tests.unit.test_backtest_market_state import (
    LIMITS,
    PERP,
    SPOT,
    T0,
    at,
    book_event,
    funding_event,
    quote_event,
)
from trading_bot.backtest.clock import ReplayClock, ReplayInvariantError
from trading_bot.backtest.events import ReplayEvent
from trading_bot.backtest.market_state import ReplayMarketData
from trading_bot.backtest.source import (
    DatasetIssues,
    DatasetRequest,
    EventValidator,
    TemporalLimits,
)
from trading_bot.exchange.models import BookLevel, MarketSpec

START = at(60_000)
REQUEST = DatasetRequest(START, START + timedelta(minutes=5), (SPOT, PERP))
SETTLES = datetime(2026, 9, 1, 8, tzinfo=T0.tzinfo)


def checker(**limits: int) -> EventValidator:
    return EventValidator(REQUEST, gap_ms=10_000, temporal=TemporalLimits(**limits))


def replace_payload(event: ReplayEvent, **changes: Any) -> ReplayEvent:
    return dataclasses.replace(event, payload=dataclasses.replace(event.payload, **changes))


class TestTemporalValidation:
    def test_a_venue_clock_ahead_of_receipt_beyond_the_skew_is_corrupt(self) -> None:
        check = checker(max_clock_skew_ms=1_000)
        ahead = quote_event(61_000, "10", "11", exchange_ms=62_001)
        assert not check.accept(ahead)
        assert check.issues.counts["future_exchange_timestamp"] == 1
        assert check.events_rejected == 1

    def test_a_small_skew_is_accepted_and_left_for_the_strategy_to_refuse(self) -> None:
        check = checker(max_clock_skew_ms=1_000)
        assert check.accept(quote_event(61_000, "10", "11", exchange_ms=61_500))

    def test_a_venue_clock_implausibly_far_behind_is_corrupt(self) -> None:
        check = checker(max_exchange_lag_ms=60_000)
        assert not check.accept(quote_event(61_000, "10", "11", exchange_ms=0))
        assert check.issues.counts["implausible_exchange_lag"] == 1

    def test_book_timestamps_are_checked_too(self) -> None:
        check = checker(max_clock_skew_ms=0)
        book = book_event(61_000, "10", "11")
        ahead = replace_payload(book, exchange_timestamp=at(61_001))
        assert not check.accept(ahead)

    def test_naive_timestamps_are_refused(self) -> None:
        check = checker()
        quote = quote_event(61_000, "10", "11")
        naive = replace_payload(quote, exchange_timestamp=datetime(2026, 9, 1, 8))
        assert not check.accept(naive)
        assert check.issues.counts["naive_timestamp"] == 1

    @pytest.mark.parametrize("hours", [0, 5, 7, 25])
    def test_a_funding_interval_no_venue_runs_is_refused(self, hours: int) -> None:
        check = checker()
        event = funding_event(61_000, settles_at=SETTLES + timedelta(hours=1))
        assert not check.accept(replace_payload(event, funding_interval_hours=hours))
        assert check.issues.counts["invalid_funding_interval"] == 1

    def test_a_settlement_already_long_past_or_beyond_one_interval_is_refused(self) -> None:
        check = checker(funding_schedule_tolerance_ms=60_000)
        past = funding_event(61_000, settles_at=at(61_000) - timedelta(minutes=5))
        assert not check.accept(past)
        beyond = funding_event(62_000, settles_at=at(62_000) + timedelta(hours=9))
        assert not check.accept(beyond)
        assert check.issues.counts["invalid_funding_schedule"] == 2
        # Just settled, reported a few seconds late: within tolerance.
        assert check.accept(funding_event(63_000, settles_at=at(63_000) - timedelta(seconds=5)))

    def test_a_settlement_schedule_that_regresses_is_refused(self) -> None:
        check = checker()
        assert check.accept(funding_event(61_000, settles_at=SETTLES + timedelta(hours=4)))
        assert not check.accept(funding_event(62_000, settles_at=SETTLES))
        assert check.issues.counts["regression"] == 1

    def test_a_non_utc_offset_is_not_naive(self) -> None:
        check = checker()
        quote = quote_event(61_000, "10", "11")
        plus_two = timezone(timedelta(hours=2))
        shifted = replace_payload(quote, exchange_timestamp=at(60_900).astimezone(plus_two))
        assert check.accept(shifted)


class TestInitialization:
    def test_the_newest_valid_observation_per_stream_is_chosen(self) -> None:
        check = checker(max_clock_skew_ms=0)
        newest_corrupt = replace_payload(
            quote_event(59_000, "10", "11"), exchange_timestamp=at(59_500)
        )
        older_valid = quote_event(58_000, "10", "11")
        book = book_event(57_000, "10", "11")
        chosen = check.initialize([newest_corrupt, older_valid, book])
        assert chosen == [book, older_valid], "replay order, the corrupt newest skipped"
        assert check.initialization_events == 2
        assert check.events_accepted == 0
        assert check.issues.counts["future_exchange_timestamp"] == 1

    def test_a_row_at_or_after_the_start_is_never_initialization(self) -> None:
        check = checker()
        assert check.initialize([quote_event(60_000, "10", "11")]) == []
        assert check.issues.counts["outside_range"] == 1

    def test_the_same_book_recorded_again_in_window_is_a_duplicate(self) -> None:
        check = checker()
        (initial,) = check.initialize([book_event(59_000, "10", "11", sequence=7)])
        assert not check.accept(book_event(60_500, "10", "11", sequence=7))
        assert check.issues.counts["duplicate"] == 1
        assert initial.available_at < START

    def test_a_gap_is_measured_from_the_state_in_force_not_from_the_start(self) -> None:
        check = checker()
        check.initialize([quote_event(59_500, "10", "11")])
        assert check.accept(quote_event(69_000, "10", "11"))  # 9.5 s after the carried quote
        assert check.issues.counts["gap"] == 0
        check.finish((SPOT,))
        assert "missing_stream" in check.issues.counts  # books never appeared at all

    def test_a_stream_present_only_as_initialization_is_not_missing(self) -> None:
        check = checker()
        check.initialize(
            [
                quote_event(59_000, "10", "11"),
                book_event(59_000, "10", "11"),
                funding_event(40_000, settles_at=SETTLES),
            ]
        )
        check.finish((SPOT,))
        assert check.issues.samples.get("missing_stream") is None

    def test_the_market_holds_the_initial_state_at_the_start_and_counts_it_apart(self) -> None:
        clock = ReplayClock(START)
        market = ReplayMarketData((SPOT, PERP), clock, LIMITS, DatasetIssues())
        market.initialize(
            [
                funding_event(20_000, settles_at=SETTLES),
                book_event(58_000, "100", "101"),
                quote_event(58_500, "100", "101"),
            ]
        )
        market.mark_exhausted()
        snapshot = market.snapshot(SPOT)
        assert snapshot.quote is not None and snapshot.book is not None
        assert snapshot.book_age_ms == 2_000
        assert market.rates[PERP].local_timestamp == at(20_000)
        assert market.initialization_applied == 3
        assert market.events_applied == 0 and market.first_applied_at is None

    def test_initialization_cannot_follow_in_window_events_or_come_from_the_future(self) -> None:
        clock = ReplayClock(START)
        market = ReplayMarketData((SPOT, PERP), clock, LIMITS, DatasetIssues())
        with pytest.raises(ReplayInvariantError, match="not before the start"):
            market.initialize([quote_event(60_000, "1", "2")])
        market.extend([quote_event(61_000, "1", "2")])
        with pytest.raises(ReplayInvariantError, match="precede"):
            market.initialize([quote_event(59_000, "1", "2")])

    def test_a_refused_read_is_recorded_even_if_the_caller_swallows_it(self) -> None:
        clock = ReplayClock(START)
        market = ReplayMarketData((SPOT, PERP), clock, LIMITS, DatasetIssues())
        market.extend([quote_event(60_000, "1", "2")])
        # What a feed reader that turns errors into "no book" does.
        with contextlib.suppress(ReplayInvariantError):
            market.snapshot(SPOT)
        assert len(market.violations) == 1


def digest(*events: ReplayEvent, roles: tuple[str, ...] = ()) -> str:
    check = checker()
    for index, event in enumerate(events):
        role = roles[index] if index < len(roles) else "event"
        if role == "init":
            check.initialize([event])
        else:
            check.accept(event)
    return check.fingerprint.hexdigest()


def fingerprint_for(request: DatasetRequest) -> str:
    return EventValidator(request, gap_ms=10_000).fingerprint.hexdigest()


QUOTE = quote_event(61_000, "10", "11", exchange_ms=60_990)
QUOTE = dataclasses.replace(QUOTE, volume_24h=Decimal(5))
BOOK = replace_payload(book_event(61_000, "10", "11"), exchange_timestamp=at(60_995))
BIDS: tuple[BookLevel, ...] = BOOK.payload.bids  # type: ignore[union-attr]
ASKS: tuple[BookLevel, ...] = BOOK.payload.asks  # type: ignore[union-attr]
FUNDING = funding_event(61_000, settles_at=SETTLES + timedelta(hours=1))

Mutation = Callable[[ReplayEvent], ReplayEvent]


def payload(**changes: Any) -> Mutation:
    return lambda event: replace_payload(event, **changes)


def envelope(**changes: Any) -> Mutation:
    return lambda event: dataclasses.replace(event, **changes)


class TestFingerprint:
    def test_request_bounds_and_markets_are_part_of_it(self) -> None:
        base = fingerprint_for(REQUEST)
        shifted = fingerprint_for(
            DatasetRequest(START + timedelta(seconds=1), REQUEST.end, REQUEST.refs)
        )
        narrowed_markets = fingerprint_for(DatasetRequest(REQUEST.start, REQUEST.end, (SPOT,)))

        assert base != shifted
        assert base != narrowed_markets

    @pytest.mark.parametrize(
        ("name", "mutate"),
        [
            ("exchange timestamp", payload(exchange_timestamp=at(60_991))),
            ("bid", payload(bid=Decimal("10.1"))),
            ("ask", payload(ask=Decimal("11.1"))),
            ("bid size", payload(bid_size=Decimal(2))),
            ("ask size", payload(ask_size=Decimal(2))),
            ("payload sequence", payload(sequence=999)),
            ("volume", envelope(volume_24h=Decimal(6))),
            ("row id", envelope(source_id=424_242)),
            ("event sequence", envelope(sequence=31_337)),
            ("symbol", envelope(ref=dataclasses.replace(SPOT, symbol="SOLUSDT"))),
        ],
    )
    def test_every_quote_field_changes_it(self, name: str, mutate: Mutation) -> None:
        assert digest(QUOTE) != digest(mutate(QUOTE)), name

    @pytest.mark.parametrize(
        ("name", "mutate"),
        [
            ("exchange timestamp", payload(exchange_timestamp=at(60_996))),
            (
                "a level's size",
                payload(bids=(BookLevel(Decimal(10), Decimal(2)), *BOOK.payload.bids[1:])),
            ),  # type: ignore[union-attr]
            (
                "a level's price",
                payload(asks=(BookLevel(Decimal(12), Decimal(1)), *BOOK.payload.asks[1:])),
            ),  # type: ignore[union-attr]
            ("one level fewer", payload(asks=BOOK.payload.asks[:-1])),  # type: ignore[union-attr]
            ("bids complete", payload(bids_complete=True)),
            ("asks complete", payload(asks_complete=True)),
            ("market type", envelope(ref=PERP)),
        ],
    )
    def test_every_book_field_changes_it(self, name: str, mutate: Mutation) -> None:
        assert digest(BOOK) != digest(mutate(BOOK)), name

    @pytest.mark.parametrize(
        ("name", "mutate"),
        [
            ("rate", payload(last_funding_rate=Decimal("0.0002"))),
            ("mark", payload(mark_price=Decimal(100_001))),
            ("index", payload(index_price=Decimal(100_001))),
            ("next settlement", payload(next_funding_time=SETTLES + timedelta(hours=2))),
            ("interval", payload(funding_interval_hours=4)),
        ],
    )
    def test_every_funding_field_changes_it(self, name: str, mutate: Mutation) -> None:
        assert digest(FUNDING) != digest(mutate(FUNDING)), name

    def test_levels_cannot_run_into_each_other(self) -> None:
        one = replace_payload(BOOK, bids=(BookLevel(Decimal(1), Decimal(12)),))
        other = replace_payload(BOOK, bids=(BookLevel(Decimal(11), Decimal(2)),))
        assert digest(one) != digest(other)

    def test_initialization_state_is_part_of_it_and_distinct_from_an_event(self) -> None:
        early = quote_event(59_000, "10", "11")
        assert digest(early, roles=("init",)) != digest()
        in_window = dataclasses.replace(early, available_at=at(61_000))
        in_window = replace_payload(in_window, local_timestamp=at(61_000))
        assert digest(early, roles=("init",)) != digest(in_window)

    def test_rejected_rows_are_part_of_it(self) -> None:
        duplicate = dataclasses.replace(QUOTE, source_id=QUOTE.source_id + 1)
        assert digest(QUOTE) != digest(QUOTE, duplicate)

    def test_a_rejected_rows_payload_is_part_of_it(self) -> None:
        duplicate = dataclasses.replace(QUOTE, source_id=QUOTE.source_id + 1)
        changed = replace_payload(duplicate, bid_size=Decimal("999"))
        assert digest(QUOTE, duplicate) != digest(QUOTE, changed)

    def test_reference_data_is_part_of_it(self) -> None:
        spec = MarketSpec(ref=SPOT, base_asset="BTC", quote_asset="USDT", is_active=True)
        tighter = dataclasses.replace(spec, min_notional=Decimal(5))
        prints = []
        for candidate, version in ((spec, "v1"), (tighter, "v1"), (spec, "v2")):
            check = checker()
            check.record_specs({SPOT: candidate}, {SPOT: version})
            prints.append(check.fingerprint.hexdigest())
        assert len(set(prints)) == 3

    def test_the_same_inputs_give_the_same_fingerprint(self) -> None:
        assert digest(QUOTE, BOOK) == digest(QUOTE, BOOK)
