"""Library Sync previews: a fresh ~30 s clip URL for a Spotify track.

Spotify id -> ISRC (cached; else one client-credentials lookup) -> Deezer track
(its id cached) -> Deezer's *current* ``preview`` URL (they're signed and expire,
so the URL itself is never cached). Only an https URL on Deezer's CDN is ever
returned, so the endpoint's redirect can't be steered anywhere else.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any, Protocol
from urllib.parse import urlsplit

SPOTIFY_ID = re.compile(r"^[A-Za-z0-9]{22}$")
_CDN_SUFFIX = ".dzcdn.net"
_META_FIELDS = ("isrc", "title", "artist", "album", "duration_s", "art")


class TrackMetaCache(Protocol):
    def get(self, spotify_id: str) -> dict[str, Any] | None: ...
    def put(self, spotify_id: str, meta: dict[str, Any]) -> None: ...
    def set_deezer_id(self, spotify_id: str, deezer_id: str | None) -> None: ...


class DeezerLookup(Protocol):
    def track_by_isrc(self, isrc: str) -> dict[str, Any] | None: ...
    def track(self, deezer_id: str) -> dict[str, Any] | None: ...


SpotifyTracks = Callable[[list[str]], list[dict[str, Any]]]


def safe_preview(url: Any) -> str | None:
    """``url`` if it's an https link on Deezer's CDN, else None."""
    if not isinstance(url, str):
        return None
    parts = urlsplit(url)
    ok = parts.scheme == "https" and (parts.hostname or "").endswith(_CDN_SUFFIX)
    return url if ok else None


def resolve_preview(
    spotify_id: str,
    *,
    cache: TrackMetaCache,
    spotify_tracks: SpotifyTracks,
    deezer: DeezerLookup,
) -> str | None:
    """The track's current Deezer preview URL, or None when there isn't one.

    Raises ``ValueError`` for anything that isn't a Spotify track id."""
    if not SPOTIFY_ID.match(spotify_id):
        raise ValueError("not a Spotify track id")
    meta = cache.get(spotify_id)
    if meta and meta.get("deezer_id"):
        fresh = deezer.track(str(meta["deezer_id"]))
        if fresh is not None:
            return safe_preview(fresh.get("preview"))
        # stale id (track pulled from Deezer) -> look it up again by ISRC
    if not meta or not meta.get("isrc"):
        found = next((t for t in spotify_tracks([spotify_id]) if t.get("id") == spotify_id), None)
        if found is None:
            return None
        meta = {k: found.get(k) for k in _META_FIELDS}
        cache.put(spotify_id, meta)
    isrc = meta.get("isrc")
    if not isrc:
        return None
    hit = deezer.track_by_isrc(str(isrc))
    cache.set_deezer_id(spotify_id, str(hit["id"]) if hit else None)
    return safe_preview(hit.get("preview")) if hit else None
