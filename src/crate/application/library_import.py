"""Per-user Spotify library import port (Phase 2).

A logged-in user can pull their own playlists + Liked Songs into crate and queue
them for FLAC download. The reader Protocol is defined here (use-case boundary);
the concrete per-user, token-refreshing implementation lives in the composition
root. Track dicts reuse the shape :class:`SpotifyClient` already emits for
playlists (``query``/``artist``/``title``/``isrc``/``duration_s``) so they flow
straight into ``RequestStore.enqueue`` exactly like a bulk import.
"""

from __future__ import annotations

from typing import Any, Protocol

from crate.domain.identity import TrackIdentity


class SpotifyLibraryReader(Protocol):
    """Reads a specific user's private Spotify library via their stored token."""

    def my_playlists(self, user_id: str) -> list[dict[str, Any]]:
        """The user's playlists as ``{id, name, count, image}`` dicts."""
        ...

    def liked_songs(self, user_id: str, max_tracks: int = 2000) -> list[dict[str, Any]]:
        """The user's Liked Songs as track dicts."""
        ...

    def playlist_tracks(
        self, user_id: str, playlist_id: str, max_tracks: int = 2000
    ) -> list[dict[str, Any]]:
        """The tracks of one of the user's playlists as track dicts."""
        ...


def to_identity(track: dict[str, Any]) -> TrackIdentity:
    """Structured identity from a Spotify track dict (artist/title/ISRC/duration),
    so imports match on ISRC + duration just like console search enqueues."""
    return TrackIdentity(
        artist=track.get("artist"),
        title=track.get("title"),
        isrc=track.get("isrc"),
        duration_s=track.get("duration_s"),
    )
