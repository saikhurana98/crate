"""SQLite listening ledger: plays, per-user recently-played cursors, and the
Spotify track-metadata cache. Same one-connection + lock discipline as the
other stores.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterable
from typing import Any

from crate.domain.listening import Play
from crate.infrastructure.sqlite.db import now_iso


class SqliteListeningStore:
    def __init__(self, conn: sqlite3.Connection, *, lock: threading.Lock | None = None) -> None:
        self._conn = conn
        self._lock = lock or threading.Lock()

    def add_plays(self, user_id: str, plays: Iterable[Play], source: str) -> int:
        """Append plays and return how many rows changed. A repeat of an existing
        (user, time, song) is a no-op, so re-polls and re-imports are idempotent.
        The one exception: an ``export`` row **replaces** a colliding ``recent``
        row (the export is authoritative; this keeps a later
        ``delete_recent_in_range`` from deleting a play the export also has)."""
        rows = [
            (
                user_id,
                p.played_at,
                p.merge_key,
                p.track_uri,
                p.ms_played,
                p.artist,
                p.title,
                p.album,
                source,
            )
            for p in plays
        ]
        if not rows:
            return 0
        with self._lock:
            cur = self._conn.executemany(
                "INSERT INTO plays(user_id, played_at, merge_key, track_uri, ms_played, "
                "artist, title, album, source) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(user_id, played_at, merge_key) DO UPDATE SET "
                "source=excluded.source, ms_played=excluded.ms_played, "
                "track_uri=COALESCE(excluded.track_uri, plays.track_uri), "
                "album=COALESCE(excluded.album, plays.album) "
                "WHERE plays.source='recent' AND excluded.source='export'",
                rows,
            )
            return max(cur.rowcount, 0)

    def recent_cursor(self, user_id: str) -> int | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT recent_after_ms FROM listen_cursors WHERE user_id=?", (user_id,)
            ).fetchone()
        return int(row["recent_after_ms"]) if row and row["recent_after_ms"] is not None else None

    def set_recent_cursor(self, user_id: str, after_ms: int | None) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO listen_cursors(user_id, recent_after_ms, last_polled_at) "
                "VALUES (?, ?, ?) ON CONFLICT(user_id) DO UPDATE SET "
                "recent_after_ms=excluded.recent_after_ms, last_polled_at=excluded.last_polled_at",
                (user_id, after_ms, now_iso()),
            )

    def last_polled_at(self, user_id: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT last_polled_at FROM listen_cursors WHERE user_id=?", (user_id,)
            ).fetchone()
        return str(row["last_polled_at"]) if row and row["last_polled_at"] else None

    def upsert_tracks(self, tracks: Iterable[dict[str, Any]]) -> None:
        """Cache Spotify track metadata. Never erases a known ISRC with an empty
        one, and never touches ``deezer_id`` (the preview cache, filled later)."""
        now = now_iso()
        rows = [
            (
                str(t["id"]),
                t.get("isrc"),
                t.get("title"),
                t.get("artist"),
                t.get("album"),
                t.get("duration_s"),
                t.get("art"),
                now,
            )
            for t in tracks
            if t.get("id")
        ]
        if not rows:
            return
        with self._lock:
            self._conn.executemany(
                "INSERT INTO spotify_tracks(id, isrc, title, artist, album, duration_s, art, "
                "fetched_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET "
                "isrc=COALESCE(excluded.isrc, spotify_tracks.isrc), title=excluded.title, "
                "artist=excluded.artist, album=excluded.album, "
                "duration_s=COALESCE(excluded.duration_s, spotify_tracks.duration_s), "
                "art=COALESCE(excluded.art, spotify_tracks.art), fetched_at=excluded.fetched_at",
                rows,
            )

    def delete_recent_in_range(self, user_id: str, start: str, end: str) -> int:
        """Drop polled plays inside an imported export's span (near-duplicates the
        export already covers). Export rows are never touched."""
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM plays WHERE user_id=? AND source='recent' "
                "AND played_at BETWEEN ? AND ?",
                (user_id, start, end),
            )
            return max(cur.rowcount, 0)

    def set_export_range(self, user_id: str, start: str, end: str) -> None:
        """Record (and widen, across several uploads) the span the export covers."""
        with self._lock:
            self._conn.execute(
                "INSERT INTO listen_cursors(user_id, export_start, export_end, export_imported_at) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(user_id) DO UPDATE SET "
                "export_start=MIN(COALESCE(listen_cursors.export_start, excluded.export_start), "
                "excluded.export_start), "
                "export_end=MAX(COALESCE(listen_cursors.export_end, excluded.export_end), "
                "excluded.export_end), export_imported_at=excluded.export_imported_at",
                (user_id, start, end, now_iso()),
            )

    def history_status(self, user_id: str) -> dict[str, Any]:
        """This user's ledger at a glance (the Sync page's history panel)."""
        with self._lock:
            agg = self._conn.execute(
                "SELECT COUNT(*) AS n, SUM(source='export') AS exported, "
                "SUM(source='recent') AS polled, MIN(played_at) AS first, "
                "MAX(played_at) AS last FROM plays WHERE user_id=?",
                (user_id,),
            ).fetchone()
            cur = self._conn.execute(
                "SELECT last_polled_at, export_start, export_end, export_imported_at "
                "FROM listen_cursors WHERE user_id=?",
                (user_id,),
            ).fetchone()
        export = None
        if cur is not None and cur["export_start"]:
            export = {
                "start": cur["export_start"],
                "end": cur["export_end"],
                "imported_at": cur["export_imported_at"],
            }
        return {
            "plays": int(agg["n"] or 0),
            "export_plays": int(agg["exported"] or 0),
            "polled_plays": int(agg["polled"] or 0),
            "first_played_at": agg["first"],
            "last_played_at": agg["last"],
            "last_polled_at": cur["last_polled_at"] if cur is not None else None,
            "export": export,
        }

    def play_count(self, user_id: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM plays WHERE user_id=?", (user_id,)
            ).fetchone()
        return int(row[0]) if row else 0

    def last_played_at(self, user_id: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT MAX(played_at) FROM plays WHERE user_id=?", (user_id,)
            ).fetchone()
        return str(row[0]) if row and row[0] else None
