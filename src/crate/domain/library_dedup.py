"""Duplicate groups and keeper ranking (pure).

Edges are evidence that two library files are the same recording:

* ``exact``: byte-identical;
* ``acoustid``: same AcoustID track, confirmed by fingerprint;
* ``fp``: fingerprint similarity alone.

Groups are the connected components once pairs marked "intentionally different"
are dropped. Within a group the copy to keep is chosen by a fixed, explainable
order of checks, and every other copy carries the reason it lost.
"""

from __future__ import annotations

import hashlib
import statistics
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass

EXACT, ACOUSTID, FP = "exact", "acoustid", "fp"
_STRENGTH = {EXACT: 3, ACOUSTID: 2, FP: 1}
VARIANT_SPREAD_S = 10.0  # radio edit vs album vs live: shown, never suggested for purge


def pair(x: str, y: str) -> tuple[str, str]:
    return (x, y) if x < y else (y, x)


@dataclass(frozen=True)
class Edge:
    a: str  # a < b
    b: str
    kind: str
    score: float

    @staticmethod
    def of(x: str, y: str, kind: str, score: float) -> Edge:
        a, b = pair(x, y)
        return Edge(a, b, kind, score)


@dataclass(frozen=True)
class Component:
    members: tuple[str, ...]  # sorted file ids
    reason: str  # the strongest evidence kind in the component
    confidence: float  # the weakest score among edges of that kind

    @property
    def signature(self) -> str:
        """Stable id: the same members give the same group id across rebuilds."""
        digest = hashlib.sha1(",".join(self.members).encode(), usedforsecurity=False)
        return digest.hexdigest()[:16]


def connected_groups(edges: Iterable[Edge], ignored: set[tuple[str, str]]) -> list[Component]:
    kept = [e for e in edges if (e.a, e.b) not in ignored]
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for e in kept:
        ra, rb = find(e.a), find(e.b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)
    by_root: dict[str, list[Edge]] = {}
    for e in kept:
        by_root.setdefault(find(e.a), []).append(e)
    out: list[Component] = []
    for group in by_root.values():
        members = tuple(sorted({x for e in group for x in (e.a, e.b)}))
        reason = max((e.kind for e in group), key=lambda k: _STRENGTH[k])
        confidence = min(e.score for e in group if e.kind == reason)
        out.append(Component(members, reason, round(confidence, 4)))
    return sorted(out, key=lambda c: c.members)


def is_variant(durations: Sequence[float]) -> bool:
    return len(durations) > 1 and max(durations) - min(durations) > VARIANT_SPREAD_S


@dataclass(frozen=True)
class FileFacts:
    id: str
    path: str
    lossless: bool
    bit_depth: int | None
    sample_rate: int | None
    bitrate: int | None
    duration_s: float | None
    tag_score: int  # how many of artist/title/album/album artist/track no. are set
    has_cover: bool
    canonical: bool  # sits where the librarian would file it
    referenced: bool  # a download record points at it


@dataclass(frozen=True)
class Ranked:
    facts: FileFacts
    rank: int  # 0 = keeper
    reasons: list[str]  # why the keeper beats this copy (empty for the keeper)


Check = tuple[Callable[[FileFacts], float], Callable[[FileFacts, FileFacts], str]]


def _khz(hz: int | None) -> str:
    return f"{(hz or 0) / 1000:g} kHz"


def _checks(reference_s: float) -> list[Check]:
    """The ranking order, highest priority first: a key (bigger is better) and how
    to say the keeper won on it."""

    def closeness(f: FileFacts) -> float:
        return -round(abs((f.duration_s or reference_s) - reference_s))

    def lossy_bitrate(f: FileFacts) -> float:
        return 0.0 if f.lossless else float(f.bitrate or 0)

    return [
        (lambda f: float(f.lossless), lambda k, m: "lossless beats lossy"),
        (
            lambda f: float(f.bit_depth or 0),
            lambda k, m: f"{k.bit_depth}-bit beats {m.bit_depth or '?'}-bit",
        ),
        (
            lambda f: float(f.sample_rate or 0),
            lambda k, m: f"{_khz(k.sample_rate)} beats {_khz(m.sample_rate)}",
        ),
        (
            lossy_bitrate,
            lambda k, m: f"{(k.bitrate or 0) // 1000} kbps beats {(m.bitrate or 0) // 1000} kbps",
        ),
        (closeness, lambda k, m: "its length matches the rest of the group"),
        (lambda f: float(f.tag_score), lambda k, m: "more complete tags"),
        (lambda f: float(f.has_cover), lambda k, m: "has embedded cover art"),
        (lambda f: float(f.canonical), lambda k, m: "sits at its canonical library path"),
        (lambda f: float(f.referenced), lambda k, m: "a download record points at it"),
    ]


def rank_files(files: Sequence[FileFacts]) -> list[Ranked]:
    """Order a group's copies best-first; ties go to the shorter, then lower path."""
    durations = [f.duration_s for f in files if f.duration_s]
    reference = statistics.median(durations) if durations else 0.0
    checks = _checks(reference)

    def key(f: FileFacts) -> tuple[object, ...]:
        return (*(-fn(f) for fn, _ in checks), len(f.path), f.path)

    ordered = sorted(files, key=key)
    keeper = ordered[0]
    out = [Ranked(keeper, 0, [])]
    for rank, f in enumerate(ordered[1:], 1):
        why = next((say(keeper, f) for fn, say in checks if fn(keeper) > fn(f)), None)
        out.append(
            Ranked(f, rank, [why] if why else ["same on every check; kept the shorter path"])
        )
    return out
