"""Canonical track metadata resolved from an authority (Spotify / MusicBrainz).

The ISRC (International Standard Recording Code) uniquely identifies a *recording*
across services, so it lets Tier 1 pick the exact master instead of a fuzzy
text match (avoiding live/remix/regional mismatches). ISRC is optional — not
every authority has it for every track.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TrackMeta:
    isrc: str | None
    title: str
    artist: str
    album: str
    duration_s: int | None
