"""Matching a Spotify track to a Navidrome song (pure), for mirroring imported
playlists into a user's Navidrome account."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from crate.domain.listening import merge_key

DURATION_TOLERANCE_S = 5.0
_LOSSLESS = frozenset({"flac", "alac", "wav", "aiff", "aif", "ape", "wv"})
_EMPTY_KEY = merge_key(None, None)


def search_query(track: Mapping[str, Any]) -> str:
    """What to ask Navidrome's search for: ``artist title``."""
    return " ".join(str(p) for p in (track.get("artist"), track.get("title")) if p).strip()


def _artists(song: Mapping[str, Any]) -> list[str]:
    """Every credited artist (OpenSubsonic ``artists``), else the display artist."""
    names = [
        str(a["name"])
        for a in song.get("artists") or []
        if isinstance(a, Mapping) and a.get("name")
    ]
    return names or [str(song.get("artist") or "")]


def _isrcs(value: Any) -> set[str]:
    items = [value] if isinstance(value, str) else list(value or [])
    return {str(v).strip().upper() for v in items if str(v).strip()}


def _rank(song: Mapping[str, Any]) -> tuple[bool, int]:
    return str(song.get("suffix") or "").lower() in _LOSSLESS, int(song.get("bitRate") or 0)


def pick_song(track: Mapping[str, Any], songs: Iterable[Mapping[str, Any]]) -> str | None:
    """The Navidrome song id of the best copy of ``track`` among search results.

    - A shared ISRC wins outright.
    - Otherwise a song matches when its artist and title normalise to the track's
      merge key (any credited artist may be the primary one) and, when both
      durations are known, they're within 5 s of each other.
    - Among several matches: lossless first, then the higher bitrate.

    ``None`` when nothing matches: better a missing song than the wrong one."""
    want_isrc = _isrcs(track.get("isrc"))
    key = merge_key(track.get("artist"), track.get("title"))
    duration = track.get("duration_s")
    hits: list[Mapping[str, Any]] = []
    for song in songs:
        if not song.get("id"):
            continue
        if want_isrc & _isrcs(song.get("isrc")):
            return str(song["id"])
        if key == _EMPTY_KEY or all(merge_key(a, song.get("title")) != key for a in _artists(song)):
            continue
        got = song.get("duration")
        if duration and got and abs(float(duration) - float(got)) > DURATION_TOLERANCE_S:
            continue
        hits.append(song)
    return str(max(hits, key=_rank)["id"]) if hits else None
