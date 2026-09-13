"""The ``spotify_tracks`` cache: Spotify id -> ISRC and display metadata, plus the
Deezer id that previews resolve through."""

from __future__ import annotations

import sqlite3
import threading
from typing import Any

from crate.infrastructure.sqlite.db import now_iso

_COLS = ("isrc", "title", "artist", "album", "duration_s", "art", "deezer_id")


class SqliteTrackMetaStore:
    def __init__(self, conn: sqlite3.Connection, *, lock: threading.Lock | None = None) -> None:
        self._conn = conn
        self._lock = lock or threading.Lock()

    def get(self, spotify_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                f"SELECT {', '.join(_COLS)} FROM spotify_tracks WHERE id=?", (spotify_id,)
            ).fetchone()
        return dict(row) if row else None

    def put(self, spotify_id: str, meta: dict[str, Any]) -> None:
        """Upsert the Spotify-side metadata; a known ``deezer_id`` is kept."""
        with self._lock:
            self._conn.execute(
                "INSERT INTO spotify_tracks(id, isrc, title, artist, album, duration_s, art, "
                "fetched_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET "
                "isrc=excluded.isrc, title=excluded.title, artist=excluded.artist, "
                "album=excluded.album, duration_s=excluded.duration_s, art=excluded.art, "
                "fetched_at=excluded.fetched_at",
                (
                    spotify_id,
                    meta.get("isrc"),
                    meta.get("title"),
                    meta.get("artist"),
                    meta.get("album"),
                    meta.get("duration_s"),
                    meta.get("art"),
                    now_iso(),
                ),
            )

    def set_deezer_id(self, spotify_id: str, deezer_id: str | None) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE spotify_tracks SET deezer_id=? WHERE id=?", (deezer_id, spotify_id)
            )
