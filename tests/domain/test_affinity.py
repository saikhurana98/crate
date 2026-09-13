"""Affinity tier maths: day cap, quarters, burst window, constant / fling /
rising rules, threshold parsing."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from crate.domain.affinity import (
    AffinityThresholds,
    AffinityTier,
    best_burst,
    cap_daily,
    classify,
    is_rising,
    parse_time,
    quarter_label,
    quarter_of,
    track_stats,
)

NOW = datetime(2026, 9, 14, tzinfo=UTC)
TH = AffinityThresholds()


def at(*ymd: int, hour: int = 12) -> datetime:
    return datetime(*ymd, hour, tzinfo=UTC)


def spread(start: datetime, n: int, every_days: int) -> list[datetime]:
    return [start + timedelta(days=i * every_days) for i in range(n)]


def test_quarter_helpers_and_parse_time() -> None:
    assert quarter_of(at(2021, 1, 1)) == "2021Q1" and quarter_of(at(2021, 12, 31)) == "2021Q4"
    assert quarter_label("2019Q2") == "Q2'19"
    assert parse_time("2021-03-04T05:06:07Z") == datetime(2021, 3, 4, 5, 6, 7, tzinfo=UTC)


def test_daily_cap_counts_a_looped_day_once_ish() -> None:
    loop = [at(2021, 1, 1, hour=h) for h in range(10)] + [at(2021, 1, 2)]
    assert len(cap_daily(loop, 3)) == 4  # 3 counted on the loop day + 1


def test_best_burst_finds_the_densest_window() -> None:
    times = sorted(spread(at(2020, 1, 1), 3, 60) + spread(at(2021, 5, 1), 9, 2))
    plays, lo, hi = best_burst(times, 30)
    assert plays == 9 and times[lo] == at(2021, 5, 1) and times[hi] == at(2021, 5, 17)
    assert best_burst([], 30) == (0, 0, 0)


def test_constant_needs_plays_quarters_and_days() -> None:
    # 4 plays in each of 4 quarters across 2 years -> a constant
    times = [t for q in range(4) for t in spread(at(2022, 1 + 3 * q, 1), 4, 5)]
    s = track_stats(times, TH)
    assert s is not None and s.active_quarters(TH.constant_quarter_plays) == 4
    assert classify(s, TH, NOW) is AffinityTier.CONSTANT


def test_two_quarters_is_not_more_than_two() -> None:
    times = [t for q in range(2) for t in spread(at(2022, 1 + 3 * q, 1), 8, 3)]
    s = track_stats(times, TH)
    assert s is not None and classify(s, TH, NOW) is not AffinityTier.CONSTANT


def test_fling_is_an_old_burst_then_silence() -> None:
    times = spread(at(2024, 3, 1), 10, 2)  # 10 plays in 20 days, 2.5 years ago
    s = track_stats(times, TH)
    assert s is not None and classify(s, TH, NOW) is AffinityTier.FLING


def test_recent_burst_is_rising_not_a_fling() -> None:
    times = spread(NOW - timedelta(days=25), 10, 2)
    s = track_stats(times, TH)
    assert s is not None and classify(s, TH, NOW) is None
    assert is_rising(s, TH, NOW) is True


def test_burst_that_kept_being_played_is_not_a_fling() -> None:
    burst = spread(at(2024, 3, 1), 10, 2)
    after = spread(at(2024, 5, 1), 6, 10)  # kept listening within 180 days
    s = track_stats(burst + after, TH)
    assert s is not None and classify(s, TH, NOW) is None


def test_burst_that_is_a_small_share_of_plays_is_not_a_fling() -> None:
    background = spread(at(2020, 1, 1), 12, 40)  # a year of occasional plays
    burst = spread(at(2024, 3, 1), 8, 3)
    s = track_stats(background + burst, TH)
    assert s is not None and s.burst_plays / s.total < TH.fling_min_share
    assert classify(s, TH, NOW) is not AffinityTier.FLING


def test_constant_beats_fling() -> None:
    steady = [
        t
        for q in range(6)
        for t in spread(at(2020, 1 + 3 * (q % 4), 1) + timedelta(days=365 * (q // 4)), 3, 7)
    ]
    s = track_stats(steady + spread(at(2023, 6, 1), 10, 2), TH)
    assert s is not None and classify(s, TH, NOW) is AffinityTier.CONSTANT


def test_track_stats_empty_is_none() -> None:
    assert track_stats([], TH) is None


def test_thresholds_round_trip_and_clamping() -> None:
    assert AffinityThresholds.from_dict(TH.to_dict()) == TH
    assert AffinityThresholds.from_dict(None) == TH
    tuned = AffinityThresholds.from_dict(
        {"constant_min_plays": "20", "fling_min_share": 7, "daily_cap": -5, "bogus": 1}
    )
    assert tuned.constant_min_plays == 20  # string coerced
    assert tuned.fling_min_share == 1.0  # clamped to [0, 1]
    assert tuned.daily_cap == 1  # clamped to >= 1
    assert AffinityThresholds.from_dict({"fling_window_days": "abc"}).fling_window_days == 30


@pytest.mark.parametrize("field", ["constant_min_plays", "fling_min_burst", "daily_cap"])
def test_int_fields_stay_ints(field: str) -> None:
    assert isinstance(getattr(AffinityThresholds.from_dict({field: 7.9}), field), int)
