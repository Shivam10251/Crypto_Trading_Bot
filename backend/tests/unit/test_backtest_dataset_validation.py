"""What a replay refuses from its dataset, and how it says so."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from trading_bot.backtest.events import EventKind, ReplayEvent
from trading_bot.backtest.source import DatasetRequest, EventValidator
from trading_bot.db.models.enums import MarketType
from trading_bot.exchange.models import FundingInfo, MarketRef, Quote

T0 = datetime(2026, 9, 1, tzinfo=UTC)
SPOT = MarketRef("replay", "ETHUSDT", MarketType.SPOT)
PERP = MarketRef("replay", "ETHUSDT", MarketType.PERPETUAL)


def quote(ms: int, sequence: int | None, source_id: int, ref: MarketRef = SPOT) -> ReplayEvent:
    moment = T0 + timedelta(milliseconds=ms)
    payload = Quote(
        ref, Decimal(10), Decimal(11), Decimal(1), Decimal(1), moment, sequence=sequence
    )
    return ReplayEvent(EventKind.QUOTE, ref, moment, source_id, payload, sequence=sequence)


def funding(ms: int, settles_ms: int, source_id: int) -> ReplayEvent:
    moment = T0 + timedelta(milliseconds=ms)
    payload = FundingInfo(
        PERP,
        Decimal(10),
        Decimal(10),
        Decimal("0.0001"),
        T0 + timedelta(milliseconds=settles_ms),
        moment,
        8,
    )
    return ReplayEvent(EventKind.FUNDING, PERP, moment, source_id, payload)


def validator(end_ms: int = 60_000, gap_ms: int = 10_000) -> EventValidator:
    request = DatasetRequest(T0, T0 + timedelta(milliseconds=end_ms), (SPOT, PERP))
    return EventValidator(request, gap_ms=gap_ms, funding_gap_ms=120_000)


class TestRefusals:
    def test_a_repeated_sequence_is_a_duplicate(self) -> None:
        check = validator()
        assert check.accept(quote(0, 5, 1))
        assert not check.accept(quote(100, 5, 2))
        assert check.issues.counts["duplicate"] == 1

    def test_a_lower_sequence_is_a_regression(self) -> None:
        check = validator()
        assert check.accept(quote(0, 5, 1))
        assert not check.accept(quote(100, 4, 2))
        assert check.issues.counts["regression"] == 1

    def test_without_sequences_the_same_receipt_time_is_a_duplicate(self) -> None:
        check = validator()
        assert check.accept(quote(0, None, 1))
        assert not check.accept(quote(0, None, 2))

    def test_a_funding_schedule_that_goes_backwards_is_a_regression(self) -> None:
        check = validator()
        assert check.accept(funding(0, 3_600_000, 1))
        assert not check.accept(funding(60_000, 1_800_000, 2))

    def test_an_event_out_of_order_is_refused_not_reordered(self) -> None:
        check = validator()
        assert check.accept(quote(1_000, 1, 1))
        assert not check.accept(quote(500, 2, 2))
        assert check.issues.counts["out_of_order"] == 1

    def test_an_event_outside_the_range_is_refused(self) -> None:
        check = validator(end_ms=1_000)
        assert not check.accept(quote(1_000, 1, 1)), "the range is half-open"


class TestGaps:
    def test_gaps_are_recorded_between_leading_and_trailing(self) -> None:
        check = validator(end_ms=60_000)
        assert check.accept(quote(15_000, 1, 1))  # leading gap of 15 s
        assert check.accept(quote(16_000, 2, 2))
        assert check.accept(quote(40_000, 3, 3))  # 24 s gap
        check.finish((SPOT,))  # trailing gap of 20 s
        assert check.issues.counts["gap"] == 3
        assert check.issues.longest_gap_ms["replay:ETHUSDT:SPOT:QUOTE"] == 24_000

    def test_funding_is_judged_against_its_polling_cadence(self) -> None:
        check = validator(end_ms=180_000)
        assert check.accept(funding(0, 3_600_000, 1))
        assert check.accept(funding(60_000, 3_600_000, 2))
        assert "gap" not in check.issues.counts

    def test_a_stream_that_never_appeared_is_named(self) -> None:
        check = validator()
        check.accept(quote(0, 1, 1))
        check.finish((SPOT, PERP))
        samples = check.issues.samples["missing_stream"]
        assert "replay:ETHUSDT:SPOT:BOOK" in samples
        assert "replay:ETHUSDT:PERPETUAL:FUNDING" in samples

    def test_samples_are_bounded_but_counts_are_not(self) -> None:
        check = validator(end_ms=10_000_000, gap_ms=1)
        for index in range(50):
            check.accept(quote(index * 10, index + 1, index + 1))
        # The first event opens the range, so 50 events leave 49 gaps.
        assert check.issues.counts["gap"] == 49
        assert len(check.issues.samples["gap"]) == 20


def test_a_request_needs_a_range_and_a_market() -> None:
    with pytest.raises(ValueError, match="end > start"):
        DatasetRequest(T0, T0, (SPOT,))
    with pytest.raises(ValueError, match="market"):
        DatasetRequest(T0, T0 + timedelta(seconds=1), ())
