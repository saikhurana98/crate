"""Bulk-download resolution: turn a pasted blob into a batch of track queries.

A batch comes from one of three origins, dispatched purely on the input text:

  - a Spotify playlist link / URI   -> resolve via the Spotify Web API
  - a YouTube playlist link          -> resolve via yt-dlp (flat playlist)
  - anything else                    -> a pasted list, one query per line

The two playlist resolvers are injected (IO lives at the edges), so this
dispatcher is unit tested without a network.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from crate.domain.identity import TrackIdentity, from_query

# ref/url -> {"name", "owner", "tracks": [str | {"query"|"artist"|"title"|...}]}
PlaylistResolver = Callable[[str], dict[str, Any]]


@dataclass(frozen=True, slots=True)
class ResolvedBatch:
    name: str
    origin: str  # spotify | youtube | paste
    owner: str
    tracks: list[TrackIdentity]


def _as_identity(track: Any) -> TrackIdentity:
    """Normalize a resolver track (rich dict or bare query string) to an identity.

    A dict carries whatever the resolver knew (artist/title/isrc/duration_s), with
    a best-effort ``from_query`` fill-in for artist/title when only a query string
    is present; a bare string is parsed with ``from_query``."""
    if isinstance(track, str):
        return from_query(track)
    query = str(track.get("query") or "").strip()
    base = from_query(query) if query else TrackIdentity()
    return TrackIdentity(
        artist=track.get("artist") or base.artist,
        title=track.get("title") or base.title,
        isrc=track.get("isrc"),
        duration_s=track.get("duration_s"),
    )


def _is_spotify_playlist(text: str) -> bool:
    return "spotify.com/playlist" in text or text.startswith("spotify:playlist:")


def _is_youtube_playlist(text: str) -> bool:
    has_list = "list=" in text or "youtube.com/playlist" in text
    return has_list and ("youtube.com" in text or "youtu.be" in text)


def resolve_bulk(
    text: str,
    *,
    spotify_playlist: PlaylistResolver | None = None,
    youtube_playlist: PlaylistResolver | None = None,
) -> ResolvedBatch:
    """Dispatch ``text`` to the right resolver and return a :class:`ResolvedBatch`.

    Raises ``LookupError`` when a playlist link is given but its resolver is
    unavailable, or ``ValueError`` when the input yields no tracks.
    """
    stripped = text.strip()
    if _is_spotify_playlist(stripped):
        if spotify_playlist is None:
            raise LookupError("Spotify is not configured — cannot resolve a Spotify playlist")
        pl = spotify_playlist(stripped)
        tracks = [_as_identity(t) for t in (pl.get("tracks") or [])]
        if not tracks:
            raise ValueError("playlist resolved to no tracks")
        return ResolvedBatch(
            str(pl.get("name") or "Playlist"), "spotify", str(pl.get("owner") or ""), tracks
        )

    if _is_youtube_playlist(stripped):
        if youtube_playlist is None:
            raise LookupError("YouTube playlist resolver is unavailable")
        pl = youtube_playlist(stripped)
        tracks = [_as_identity(t) for t in (pl.get("tracks") or [])]
        if not tracks:
            raise ValueError("playlist resolved to no tracks")
        return ResolvedBatch(str(pl.get("name") or "YouTube playlist"), "youtube", "", tracks)

    # pasted list — one identity per non-empty line (title-only, best-effort split)
    lines = [ln.strip() for ln in stripped.splitlines() if ln.strip()]
    if not lines:
        raise ValueError("nothing to enqueue")
    name = lines[0] if len(lines) == 1 else f"Pasted list · {len(lines)} tracks"
    return ResolvedBatch(name, "paste", "", [from_query(ln) for ln in lines])
