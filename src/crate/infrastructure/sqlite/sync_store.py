"""SQLite storage for Library Sync: playlist sync-state (``sync_playlists`` +
their last-fetched items) and the auto-sync ledger + queue."""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from crate.application.library_import import to_identity
from crate.application.library_sync import SYNC_MODES, track_key
from crate.infrastructure.sqlite.db import now_iso
from crate.infrastructure.sqlite.store import RequestStore

_ITEM_FIELDS = ("id", "artist", "title", "album", "isrc", "duration_s", "query", "art")


class SqlitePlaylistSyncStore:
    def __init__(self, conn: sqlite3.Connection, *, lock: threading.Lock | None = None) -> None:
        self._conn = conn
        self._lock = lock or threading.Lock()

    def playlists(self, user_id: str, *, include_removed: bool = False) -> list[dict[str, Any]]:
        """Liked Songs first, then owned playlists, then followed, by name."""
        sql = "SELECT * FROM sync_playlists WHERE user_id=?"
        if not include_removed:
            sql += " AND removed_at IS NULL"
        sql += " ORDER BY spotify_id != 'liked', owned DESC, name COLLATE NOCASE"
        with self._lock:
            rows = [dict(r) for r in self._conn.execute(sql, (user_id,))]
        for r in rows:
            r["owned"], r["generated"] = bool(r["owned"]), bool(r["generated"])
        return rows

    def save_playlist(self, user_id: str, row: Mapping[str, Any], *, changed: bool) -> None:
        """Upsert a playlist's metadata. Seeing it again clears ``removed_at``;
        ``changed`` stamps ``last_changed_at``. ``sync_mode`` is never touched."""
        now = now_iso()
        with self._lock:
            self._conn.execute(
                "INSERT INTO sync_playlists(user_id, spotify_id, name, owner_id, owned, generated, "
                "image, snapshot_id, last_changed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(user_id, spotify_id) DO UPDATE SET name=excluded.name, "
                "owner_id=excluded.owner_id, owned=excluded.owned, generated=excluded.generated, "
                "image=excluded.image, snapshot_id=excluded.snapshot_id, removed_at=NULL, "
                "last_changed_at=COALESCE(?, sync_playlists.last_changed_at)",
                (
                    user_id,
                    row["spotify_id"],
                    row["name"],
                    row.get("owner_id"),
                    1 if row.get("owned") else 0,
                    1 if row.get("generated") else 0,
                    row.get("image"),
                    row.get("snapshot_id"),
                    now,
                    now if changed else None,
                ),
            )

    def set_items(self, user_id: str, spotify_id: str, tracks: Sequence[Mapping[str, Any]]) -> None:
        """Replace the stored track list (kept so an unchanged playlist isn't refetched)."""
        rows = [
            (
                user_id,
                spotify_id,
                pos,
                track_key(dict(t)),
                json.dumps({k: t.get(k) for k in _ITEM_FIELDS}),
            )
            for pos, t in enumerate(tracks)
        ]
        with self._lock:
            self._conn.execute(
                "DELETE FROM sync_playlist_items WHERE user_id=? AND spotify_id=?",
                (user_id, spotify_id),
            )
            self._conn.executemany(
                "INSERT INTO sync_playlist_items(user_id, spotify_id, position, track_key, "
                "track_json) VALUES (?, ?, ?, ?, ?)",
                rows,
            )

    def items(self, user_id: str, spotify_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT track_json FROM sync_playlist_items WHERE user_id=? AND spotify_id=? "
                "ORDER BY position",
                (user_id, spotify_id),
            ).fetchall()
        return [json.loads(r[0]) for r in rows]

    def set_counts(
        self, user_id: str, spotify_id: str, *, track_count: int, in_library: int
    ) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE sync_playlists SET track_count=?, in_library_count=?, last_checked_at=? "
                "WHERE user_id=? AND spotify_id=?",
                (track_count, in_library, now_iso(), user_id, spotify_id),
            )

    def mark_removed(self, user_id: str, present: set[str]) -> list[str]:
        """Stamp ``removed_at`` on playlists no longer in the user's library.
        Returns their names. Nothing is deleted: the row and its mode survive a re-follow."""
        with self._lock:
            live = self._conn.execute(
                "SELECT spotify_id, name FROM sync_playlists "
                "WHERE user_id=? AND removed_at IS NULL",
                (user_id,),
            ).fetchall()
            gone = [r for r in live if r["spotify_id"] not in present]
            self._conn.executemany(
                "UPDATE sync_playlists SET removed_at=? WHERE user_id=? AND spotify_id=?",
                [(now_iso(), user_id, r["spotify_id"]) for r in gone],
            )
        return [str(r["name"]) for r in gone]

    def set_sync_mode(self, user_id: str, spotify_id: str, mode: str) -> bool:
        """Set a playlist's auto-sync mode (inherit | on | off). False if unknown."""
        if mode not in SYNC_MODES:
            raise ValueError(f"sync mode must be one of {', '.join(SYNC_MODES)}")
        with self._lock:
            cur = self._conn.execute(
                "UPDATE sync_playlists SET sync_mode=? WHERE user_id=? AND spotify_id=?",
                (mode, user_id, spotify_id),
            )
        return cur.rowcount == 1


class SqliteSyncQueue:
    """Auto-sync's ledger and enqueue. A ``(user, scope, key)`` is queued or
    baselined at most once. Queued requests go into one ``origin='sync'`` batch
    per scope per run, attributed to the user, at priority 1."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        requests: RequestStore,
        *,
        lock: threading.Lock | None = None,
    ) -> None:
        self._conn = conn
        self._requests = requests
        self._lock = lock or threading.Lock()

    def ledger_keys(self, user_id: str, scope: str) -> set[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT track_key FROM sync_ledger WHERE user_id=? AND scope=?", (user_id, scope)
            ).fetchall()
        return {str(r[0]) for r in rows}

    def queued_keys(self, user_id: str) -> set[str]:
        """Keys auto-sync already queued for this user, under any scope."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT track_key FROM sync_ledger "
                "WHERE user_id=? AND request_id IS NOT NULL",
                (user_id,),
            ).fetchall()
        return {str(r[0]) for r in rows}

    def baseline(self, user_id: str, scope: str, keys: Iterable[str]) -> int:
        """Record ``keys`` as seen without queueing them. Returns how many were new."""
        now = now_iso()
        rows = [(user_id, scope, key, now) for key in sorted(set(keys))]
        with self._lock:
            cur = self._conn.executemany(
                "INSERT OR IGNORE INTO sync_ledger(user_id, scope, track_key, request_id, "
                "enqueued_at) VALUES (?, ?, ?, NULL, ?)",
                rows,
            )
        return max(cur.rowcount, 0)

    def enqueue_tracks(
        self,
        user_id: str,
        scope: str,
        name: str,
        items: Sequence[tuple[str, Mapping[str, Any]]],
    ) -> int:
        if not items:
            return 0
        bid = self._requests.create_batch(name, "sync", name, len(items), user_id=user_id)
        for key, track in items:
            fallback = f"{track.get('artist') or ''} {track.get('title') or ''}".strip()
            rid = self._requests.enqueue(
                str(track.get("query") or fallback),
                bid,
                to_identity(dict(track)),
                user_id=user_id,
                priority=1,
            )
            with self._lock:
                self._conn.execute(
                    "INSERT OR REPLACE INTO sync_ledger(user_id, scope, track_key, request_id, "
                    "enqueued_at) VALUES (?, ?, ?, ?, ?)",
                    (user_id, scope, key, rid, now_iso()),
                )
        return len(items)
