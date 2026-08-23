"""Continuous playlist sync (Phase D) — pure use-case core.

The watcher (infrastructure) polls each active synced playlist, and the moment its
Spotify ``snapshot_id`` changes it asks this module which tracks are *new* since the
last poll. Everything here is zero-I/O and unit-tested: the identity key for a track
and the diff against the already-enqueued ledger.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any, Protocol

from crate.domain.matching import norm_tokens


def track_key(track: dict[str, Any]) -> str:
    """A stable dedupe key for a playlist track: its ISRC when present (the
    strongest identity), else the normalized artist+title tokens (the same
    normalizer the cascade's matcher uses). Keyed strings are prefixed so an ISRC
    can never collide with a token key. Empty/uninformative tracks collapse to
    ``tok:`` — the watcher skips those before enqueueing."""
    isrc = str(track.get("isrc") or "").strip()
    if isrc:
        return f"isrc:{isrc.upper()}"
    tokens = norm_tokens(str(track.get("artist") or "")) + norm_tokens(
        str(track.get("title") or "")
    )
    return "tok:" + " ".join(tokens)


def diff_playlist(known: set[str], current: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """The tracks in ``current`` whose :func:`track_key` isn't already in ``known``.

    Preserves playlist order, de-dupes within the current snapshot (a track listed
    twice enqueues once), and drops uninformative rows (an empty ``tok:`` key — no
    ISRC and no usable artist/title). ``known`` is the enqueued-ledger key set."""
    seen = set(known)
    out: list[dict[str, Any]] = []
    for track in current:
        key = track_key(track)
        if key == "tok:" or key in seen:
            continue
        seen.add(key)
        out.append(track)
    return out


class SyncedPlaylistStore(Protocol):
    """Persistence the watcher drives: the subscriptions + the per-playlist ledger
    of already-enqueued track keys (implemented by :class:`RequestStore`)."""

    def list_synced_playlists(
        self, *, active_only: bool = False, user_id: str | None = None
    ) -> list[dict[str, Any]]:
        """Synced-playlist rows, newest first; optionally only active ones / one user's."""
        ...

    def synced_track_keys(self, synced_playlist_id: str) -> set[str]:
        """The set of track keys already enqueued for this playlist (the ledger)."""
        ...

    def record_synced_tracks(self, synced_playlist_id: str, keys: Iterable[str]) -> None:
        """Append keys to the ledger (idempotent — INSERT OR IGNORE)."""
        ...

    def mark_synced_checked(self, synced_playlist_id: str, snapshot_id: str | None) -> None:
        """Stamp last_checked_at now and store the freshly-seen snapshot_id."""
        ...


class PlaylistSource(Protocol):
    """The watcher's read side over a user's Spotify (implemented by SpotifyUser):
    a cheap snapshot probe + the full track list when it changes."""

    def playlist_snapshot(self, user_id: str, playlist_id: str) -> str | None:
        """The playlist's current snapshot id (``None`` if gone/unreadable)."""
        ...

    def playlist_tracks(
        self, user_id: str, playlist_id: str, max_tracks: int = 2000
    ) -> list[dict[str, Any]]:
        """The playlist's tracks as identity dicts (query/artist/title/isrc/duration_s)."""
        ...


# Enqueue one newly-added track for a user (wired in composition to
# RequestStore.enqueue + to_identity). Kept as a callable so this module stays
# free of any infrastructure import.
EnqueueNew = Callable[[dict[str, Any], str], None]


def sync_tick(
    store: SyncedPlaylistStore,
    source: PlaylistSource,
    enqueue: EnqueueNew,
    *,
    log: Callable[[str], None] | None = None,
) -> int:
    """One sweep over every active subscription; returns how many tracks it queued.

    Per playlist:

    - Probe the current ``snapshot_id``. If it matches the stored one, the playlist
      is unchanged — stamp the check time and move on without pulling tracks.
    - Otherwise fetch the tracks. On the **first** poll of a subscription (no stored
      snapshot) seed the ledger with the current contents but enqueue **nothing** —
      sync tracks *additions*; the existing contents are what the Import button is
      for. On later polls, enqueue only tracks whose key isn't in the ledger, then
      extend the ledger and store the new snapshot.

    One playlist's failure (token expiry, deleted playlist) is logged and skipped so
    the rest of the sweep still runs."""
    queued = 0
    for row in store.list_synced_playlists(active_only=True):
        sid, user_id, ref = str(row["id"]), str(row["user_id"]), str(row["spotify_ref"])
        try:
            current = source.playlist_snapshot(user_id, ref)
            if current is not None and current == row.get("snapshot_id"):
                store.mark_synced_checked(sid, current)  # unchanged — just touch the clock
                continue
            tracks = source.playlist_tracks(user_id, ref)
            first_poll = not row.get("snapshot_id")
            if first_poll:
                # Baseline: remember what's already there, download none of it.
                store.record_synced_tracks(sid, [track_key(t) for t in tracks])
            else:
                new = diff_playlist(store.synced_track_keys(sid), tracks)
                for track in new:
                    enqueue(track, user_id)
                store.record_synced_tracks(sid, [track_key(t) for t in new])
                queued += len(new)
                if new and log is not None:
                    log(f"playlist-sync: queued {len(new)} new track(s) from {row.get('name')!r}")
            store.mark_synced_checked(sid, current)
        except Exception as exc:  # one bad playlist must not stop the sweep
            if log is not None:
                log(f"playlist-sync: skipped {ref} for {user_id}: {exc}")
    return queued
