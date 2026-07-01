"""MusicBrainz metadata resolver — free-text ISRC lookup.

Fallback authority when the query is not a Spotify link. MusicBrainz has no
rate-limit token dance (just a required User-Agent), and returns per-recording
ISRCs. Coverage is imperfect and a recording may carry several ISRCs, so this
is best-effort — the caller treats a returned ISRC as a *preference*, not a
requirement. HTTP is injected; selection logic is pure.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from lbmd.domain.meta import TrackMeta

_URL = "https://musicbrainz.org/ws/2/recording"
_UA = "lbmd/0.0 (https://github.com/saikhurana98/load-balanced-music-downloader)"
_MIN_SCORE = 90

GetJson = Callable[..., Any]


def resolve(query: str, *, get_json: GetJson, min_score: int = _MIN_SCORE) -> TrackMeta | None:
    """Best-effort ISRC + metadata for free text via MusicBrainz recording search."""
    data = get_json(
        _URL,
        {"query": query, "fmt": "json", "limit": 1, "inc": "isrcs"},
        {"User-Agent": _UA},
    )
    recordings = data.get("recordings", [])
    if not recordings:
        return None
    top = recordings[0]
    if top.get("score", 0) < min_score:
        return None
    isrcs = top.get("isrcs") or []
    credit = top.get("artist-credit") or []
    artist = credit[0].get("name", "") if credit else ""
    length = top.get("length")
    return TrackMeta(
        isrc=isrcs[0] if isrcs else None,
        title=str(top.get("title", "Unknown")),
        artist=str(artist),
        album="",
        duration_s=round(length / 1000) if length else None,
    )
