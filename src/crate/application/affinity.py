"""Affinity-tier use-cases: turn a user's play ledger (or, before the export,
their Spotify top-tracks lists) into tier-2/3 members, and diff membership
between sync runs. Pure: stores and Spotify are the caller's.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from crate.domain.affinity import (
    AffinityThresholds,
    AffinityTier,
    TrackStats,
    classify,
    is_rising,
    parse_time,
    quarter_label,
    track_stats,
)
from crate.domain.listening import merge_key

TOP_RANGES = ("short_term", "medium_term", "long_term")


@dataclass(frozen=True)
class TrackPlays:
    """One song's slice of the ledger (built by the listening store)."""

    merge_key: str
    played_at: list[str]
    artist: str | None
    title: str | None
    track_uri: str | None  # the most-played Spotify uri; None for local files


@dataclass(frozen=True)
class TierMember:
    tier: AffinityTier
    merge_key: str
    spotify_id: str | None
    artist: str | None
    title: str | None
    score: float
    stats: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ListeningTiers:
    constants: list[TierMember]
    flings: list[TierMember]
    rising: list[TierMember]  # flings in progress: a hint only, never auto-synced
    approximate: bool = False


def spotify_id_of(uri: str | None) -> str | None:
    """``spotify:track:ID`` -> ``ID`` (anything else, e.g. local files -> None)."""
    return uri.rsplit(":", 1)[-1] if uri and uri.startswith("spotify:track:") else None


def summarize(s: TrackStats, th: AffinityThresholds) -> dict[str, Any]:
    """A JSON-safe stats card for the UI, e.g. ``41 plays, Q2'19-Q3'24``."""
    ordered = sorted(s.quarters)
    return {
        "plays": s.total,
        "raw_plays": s.raw_total,
        "days": s.distinct_days,
        "active_quarters": s.active_quarters(th.constant_quarter_plays),
        "span": f"{quarter_label(ordered[0])}-{quarter_label(ordered[-1])}",
        "first": s.first.isoformat(),
        "last": s.last.isoformat(),
        "burst_plays": s.burst_plays,
        "burst_start": s.burst_start.isoformat(),
        "burst_end": s.burst_end.isoformat(),
        "after_burst": s.after_burst,
    }


def _rank(members: list[TierMember]) -> list[TierMember]:
    return sorted(members, key=lambda m: (-m.score, m.artist or "", m.title or ""))


def compute_listening_tiers(
    tracks: Iterable[TrackPlays], th: AffinityThresholds, now: datetime
) -> ListeningTiers:
    """Classify every song in the ledger. Constants rank by counted plays, and
    flings (and rising bursts) by burst size."""
    constants: list[TierMember] = []
    flings: list[TierMember] = []
    rising: list[TierMember] = []
    for tp in tracks:
        stats = track_stats([parse_time(t) for t in tp.played_at], th)
        if stats is None:
            continue
        card = summarize(stats, th) | {"local": tp.track_uri is None}
        tier = classify(stats, th, now)

        def member(
            t: AffinityTier, score: float, tp: TrackPlays = tp, card: dict[str, Any] = card
        ) -> TierMember:
            sid = spotify_id_of(tp.track_uri)
            return TierMember(t, tp.merge_key, sid, tp.artist, tp.title, score, card)

        if tier is AffinityTier.CONSTANT:
            constants.append(member(tier, float(stats.total)))
        elif tier is AffinityTier.FLING:
            flings.append(member(tier, float(stats.burst_plays)))
        elif is_rising(stats, th, now):
            rising.append(member(AffinityTier.FLING, float(stats.burst_plays)))
    return ListeningTiers(_rank(constants), _rank(flings), _rank(rising))


def approximate_tiers(top: Mapping[str, Sequence[Mapping[str, Any]]]) -> ListeningTiers:
    """Tiers from Spotify's top-tracks lists, until the history export arrives.

    * constant ≈ in both the ~1-year (long) and ~6-month (medium) lists;
    * fling ≈ in the medium list, but in neither the long nor the ~4-week
      (short) list: hot in the last few months, and gone again now.

    List position becomes the score, so earlier tracks rank higher."""

    def key(t: Mapping[str, Any]) -> str:
        return merge_key(t.get("artist"), t.get("title"))

    short_keys = {key(t) for t in top.get("short_term", [])}
    medium = list(top.get("medium_term", []))
    long_ = list(top.get("long_term", []))
    medium_keys = {key(t) for t in medium}
    long_keys = {key(t) for t in long_}

    def members(tier: AffinityTier, source: list[Mapping[str, Any]], keep: Any) -> list[TierMember]:
        out: list[TierMember] = []
        seen: set[str] = set()
        for rank, t in enumerate(source):
            k = key(t)
            if k in seen or not keep(k):
                continue
            seen.add(k)
            out.append(
                TierMember(
                    tier,
                    k,
                    str(t["id"]) if t.get("id") else None,
                    t.get("artist"),
                    t.get("title"),
                    float(len(source) - rank),
                    {"approximate": True, "rank": rank + 1},
                )
            )
        return out

    constants = members(AffinityTier.CONSTANT, long_, lambda k: k in medium_keys)
    flings = members(
        AffinityTier.FLING, medium, lambda k: k not in long_keys and k not in short_keys
    )
    return ListeningTiers(constants, flings, [], approximate=True)


def diff_members(previous: Iterable[str], current: Iterable[str]) -> tuple[set[str], set[str]]:
    """``(entered, left)`` between two membership snapshots."""
    prev, cur = set(previous), set(current)
    return cur - prev, prev - cur
