"""Listening-affinity tiers (pure): which songs are **long-term constants**
(tier 2) and which were **short-term flings** (tier 3), judged from a song's
play times.

Tier 1 (Liked Songs + your/followed playlists) comes straight from Spotify and
isn't computed here. Every rule is a tunable :class:`AffinityThresholds` field,
stored per user. The defaults follow the definitions agreed in the plan:

* **constant**: at least ``constant_min_plays`` counted plays; at least
  ``constant_min_quarters`` quarters with ``constant_quarter_plays`` plays each
  ("more than two quarters"); and plays on ``constant_min_days`` distinct days.
* **fling**:
  * the densest ``fling_window_days`` window holds ``fling_min_burst`` plays;
  * that is at least ``fling_min_share`` of all the song's plays;
  * the song was mostly forgotten afterwards (at most
    ``max(fling_after_min_abs, fling_after_max_share * burst)`` plays in the
    next ``fling_after_days``);
  * the burst ended at least ``fling_cooldown_days`` ago.
  A burst that is still going is only a "rising" hint.

Plays are **capped at ``daily_cap`` per UTC day**, so a song looped all day
counts as one day of listening, not forty plays.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, fields
from datetime import UTC, datetime, timedelta
from enum import IntEnum
from typing import Any


class AffinityTier(IntEnum):
    CORE = 1  # Liked Songs + your/followed playlists
    CONSTANT = 2  # long-term constants
    FLING = 3  # short-term flings


@dataclass(frozen=True)
class AffinityThresholds:
    daily_cap: int = 3
    constant_min_plays: int = 12
    constant_min_quarters: int = 3
    constant_quarter_plays: int = 2
    constant_min_days: int = 6
    fling_window_days: int = 30
    fling_min_burst: int = 8
    fling_min_share: float = 0.6
    fling_after_days: int = 180
    fling_after_max_share: float = 0.15
    fling_after_min_abs: int = 2
    fling_cooldown_days: int = 60

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any] | None) -> AffinityThresholds:
        """Build from stored or user-supplied JSON. Unknown keys are ignored,
        unparseable values keep their default, and everything is clamped to a
        sane range, so a bad slider can't produce nonsense tiers."""
        base = cls()
        if not raw:
            return base
        values = asdict(base)
        for f in fields(cls):
            if f.name not in raw:
                continue
            lo, hi = _BOUNDS[f.name]
            try:
                parsed = float(raw[f.name])
            except (TypeError, ValueError):
                continue
            clamped = min(max(parsed, lo), hi)
            values[f.name] = clamped if isinstance(values[f.name], float) else int(clamped)
        return cls(**values)


_BOUNDS: dict[str, tuple[float, float]] = {
    "daily_cap": (1, 50),
    "constant_min_plays": (1, 10_000),
    "constant_min_quarters": (1, 200),
    "constant_quarter_plays": (1, 1_000),
    "constant_min_days": (1, 10_000),
    "fling_window_days": (1, 365),
    "fling_min_burst": (1, 10_000),
    "fling_min_share": (0.0, 1.0),
    "fling_after_days": (1, 3_650),
    "fling_after_max_share": (0.0, 1.0),
    "fling_after_min_abs": (0, 10_000),
    "fling_cooldown_days": (0, 3_650),
}


def parse_time(ts: str) -> datetime:
    """A ledger timestamp (``YYYY-MM-DDTHH:MM:SSZ``) as an aware UTC datetime."""
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(UTC)


def quarter_of(dt: datetime) -> str:
    """Calendar quarter key, e.g. ``2021Q3`` (UTC)."""
    return f"{dt.year}Q{(dt.month - 1) // 3 + 1}"


def quarter_label(key: str) -> str:
    """``2021Q3`` -> ``Q3'21`` (compact, for the UI)."""
    year, q = key.split("Q")
    return f"Q{q}'{year[-2:]}"


def cap_daily(times: Sequence[datetime], cap: int) -> list[datetime]:
    """Sorted plays with at most ``cap`` counted per UTC day."""
    per_day: Counter[Any] = Counter()
    out: list[datetime] = []
    for t in sorted(times):
        day = t.date()
        if per_day[day] < cap:
            per_day[day] += 1
            out.append(t)
    return out


def best_burst(times: Sequence[datetime], window_days: int) -> tuple[int, int, int]:
    """``(plays, start_index, end_index)`` of the densest ``window_days`` span in
    sorted ``times``: a two-pointer sweep, O(n). The earliest densest span wins ties."""
    window = timedelta(days=window_days)
    best = (0, 0, 0)
    lo = 0
    for hi, t in enumerate(times):
        while t - times[lo] > window:
            lo += 1
        if hi - lo + 1 > best[0]:
            best = (hi - lo + 1, lo, hi)
    return best


@dataclass(frozen=True)
class TrackStats:
    total: int  # day-capped counted plays
    raw_total: int
    distinct_days: int
    quarters: dict[str, int]  # quarter key -> counted plays
    first: datetime
    last: datetime
    burst_plays: int
    burst_start: datetime
    burst_end: datetime
    after_burst: int  # counted plays in the fling_after_days after the burst

    def active_quarters(self, min_plays: int) -> int:
        return sum(1 for n in self.quarters.values() if n >= min_plays)


def track_stats(times: Sequence[datetime], th: AffinityThresholds) -> TrackStats | None:
    """The numbers the tier rules read, from one song's play times."""
    if not times:
        return None
    capped = cap_daily(times, th.daily_cap)
    plays, lo, hi = best_burst(capped, th.fling_window_days)
    burst_end = capped[hi]
    horizon = burst_end + timedelta(days=th.fling_after_days)
    return TrackStats(
        total=len(capped),
        raw_total=len(times),
        distinct_days=len({t.date() for t in capped}),
        quarters=dict(Counter(quarter_of(t) for t in capped)),
        first=capped[0],
        last=capped[-1],
        burst_plays=plays,
        burst_start=capped[lo],
        burst_end=burst_end,
        after_burst=sum(1 for t in capped[hi + 1 :] if t <= horizon),
    )


def is_constant(s: TrackStats, th: AffinityThresholds) -> bool:
    return (
        s.total >= th.constant_min_plays
        and s.active_quarters(th.constant_quarter_plays) >= th.constant_min_quarters
        and s.distinct_days >= th.constant_min_days
    )


def _burst_qualifies(s: TrackStats, th: AffinityThresholds) -> bool:
    return s.burst_plays >= th.fling_min_burst and s.burst_plays >= th.fling_min_share * s.total


def is_fling(s: TrackStats, th: AffinityThresholds, now: datetime) -> bool:
    if not _burst_qualifies(s, th) or (now - s.burst_end).days < th.fling_cooldown_days:
        return False
    limit = max(th.fling_after_min_abs, th.fling_after_max_share * s.burst_plays)
    return s.after_burst <= limit


def is_rising(s: TrackStats, th: AffinityThresholds, now: datetime) -> bool:
    """A fling in progress: the burst qualifies but hasn't cooled off yet."""
    return _burst_qualifies(s, th) and (now - s.burst_end).days < th.fling_cooldown_days


def classify(s: TrackStats, th: AffinityThresholds, now: datetime) -> AffinityTier | None:
    """CONSTANT beats FLING: a long-term favourite that also had one intense
    month is a constant."""
    if is_constant(s, th):
        return AffinityTier.CONSTANT
    if is_fling(s, th, now):
        return AffinityTier.FLING
    return None
