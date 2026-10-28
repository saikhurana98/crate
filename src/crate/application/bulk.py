"""Bulk-download resolution: turn a pasted blob into a batch of track queries.

A batch comes from one of four origins, dispatched purely on the input text:

  - a Spotify playlist link / URI   -> resolve via the Spotify Web API
  - a YouTube playlist link          -> resolve via yt-dlp (flat playlist)
  - an Octave album link             -> resolve via Octave's public catalog
  - anything else                    -> a pasted list, one query per line

The playlist and album resolvers are injected (IO lives at the edges), so this
dispatcher is unit tested without a network.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlsplit

from crate.domain.identity import TrackIdentity, from_query

# ref/url -> {"name", "owner", "tracks": [str | {"query"|"artist"|"title"|...}]}
PlaylistResolver = Callable[[str], dict[str, Any]]


@dataclass(frozen=True, slots=True)
class ResolvedBatch:
    name: str
    origin: str  # spotify | youtube | octave | paste
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
    if text.startswith("spotify:playlist:"):
        return True
    try:
        url = urlsplit(text)
    except ValueError:
        return False
    return (
        url.scheme == "https"
        and (url.hostname or "").lower() == "open.spotify.com"
        and "/playlist/" in url.path
    )


def _is_youtube_playlist(text: str) -> bool:
    try:
        url = urlsplit(text)
    except ValueError:
        return False
    host = (url.hostname or "").lower()
    is_youtube = host in {
        "youtube.com",
        "www.youtube.com",
        "m.youtube.com",
        "music.youtube.com",
        "youtu.be",
    }
    has_list = "list" in parse_qs(url.query) or url.path.rstrip("/") == "/playlist"
    return url.scheme == "https" and is_youtube and has_list


def _is_octave_album(text: str) -> bool:
    try:
        url = urlsplit(text)
    except ValueError:
        return False
    return (
        url.scheme == "https"
        and (url.hostname or "").lower() == "music.octavestreaming.com"
        and url.path.startswith("/album/")
    )


def resolve_bulk(
    text: str,
    *,
    spotify_playlist: PlaylistResolver | None = None,
    youtube_playlist: PlaylistResolver | None = None,
    octave_album: PlaylistResolver | None = None,
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

    if _is_octave_album(stripped):
        if octave_album is None:
            raise LookupError("Octave album resolver is unavailable")
        album = octave_album(stripped)
        tracks = [_as_identity(track) for track in (album.get("tracks") or [])]
        if not tracks:
            raise ValueError("Octave album resolved to no tracks")
        return ResolvedBatch(
            str(album.get("name") or "Octave album"),
            "octave",
            str(album.get("owner") or ""),
            tracks,
        )

    # pasted list — one identity per non-empty line (title-only, best-effort split)
    lines = [ln.strip() for ln in stripped.splitlines() if ln.strip()]
    if not lines:
        raise ValueError("nothing to enqueue")
    name = lines[0] if len(lines) == 1 else f"Pasted list · {len(lines)} tracks"
    return ResolvedBatch(name, "paste", "", [from_query(ln) for ln in lines])
