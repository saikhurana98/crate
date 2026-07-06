"""Tier-cascade domain logic (ADR-0015).

Pure, dependency-free. Turns the set of currently-available sources into an
ordered cascade plan: a list of race groups, one per non-empty tier, in strict
priority order. The application scheduler races the sources *within* a group in
parallel and only advances to the next group when the current one is exhausted.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import IntEnum


class Tier(IntEnum):
    """Source priority tier. Lower value = higher priority (tried first)."""

    ONE = 1  # ripping sites -> 24-bit FLAC (Squid/Qobuz, Lucida, Monochrome, Soulseek)
    TWO = 2  # streaming lossless rippers (Spotify / Apple Music) -> FLAC
    THREE = 3  # yt-dlp / equivalent -> 320 kbps lossy fallback


@dataclass(frozen=True, slots=True)
class SourceRef:
    """An available source and its eligibility for this request."""

    name: str
    tier: Tier
    healthy: bool = True


def plan_cascade(sources: Iterable[SourceRef]) -> list[list[SourceRef]]:
    """Group healthy sources into per-tier race sets, ordered by tier priority.

    Unhealthy sources are excluded. Tiers with no healthy sources are skipped
    entirely (never yield an empty group). Within a tier, sources are ordered
    deterministically by name so the plan is reproducible.
    """
    healthy = [s for s in sources if s.healthy]
    plan: list[list[SourceRef]] = []
    for tier in Tier:
        group = sorted((s for s in healthy if s.tier is tier), key=lambda s: s.name)
        if group:
            plan.append(group)
    return plan
