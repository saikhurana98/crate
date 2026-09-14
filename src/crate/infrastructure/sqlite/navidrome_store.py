"""Per-user Navidrome accounts, the playlists mirrored into them, and a shared
merge key -> Navidrome song id cache. Passwords are encrypted with
:class:`SecretBox` and only ever decrypted for the push job's Subsonic calls."""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterable
from typing import Any

from crate.infrastructure.crypto import SecretBox
from crate.infrastructure.sqlite.db import now_iso

_CHUNK = 500  # SQLite's bound-parameter limit is far above this; keeps statements small


class SqliteNavidromeStore:
    def __init__(
        self, conn: sqlite3.Connection, box: SecretBox, *, lock: threading.Lock | None = None
    ) -> None:
        self._conn = conn
        self._box = box
        self._lock = lock or threading.Lock()

    # --- accounts ---
    def set_account(self, user_id: str, username: str, password: str) -> None:
        now = now_iso()
        with self._lock:
            self._conn.execute(
                "INSERT INTO navidrome_accounts(user_id, username, password_ct, verified_at, "
                "updated_at) VALUES (?, ?, ?, ?, ?) ON CONFLICT(user_id) DO UPDATE SET "
                "username=excluded.username, password_ct=excluded.password_ct, "
                "verified_at=excluded.verified_at, updated_at=excluded.updated_at",
                (user_id, username, self._box.encrypt(password), now, now),
            )

    def account(self, user_id: str) -> dict[str, Any] | None:
        """``{username, verified_at}``; never the password."""
        with self._lock:
            row = self._conn.execute(
                "SELECT username, verified_at FROM navidrome_accounts WHERE user_id=?", (user_id,)
            ).fetchone()
        return dict(row) if row else None

    def credentials(self, user_id: str) -> tuple[str, str] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT username, password_ct FROM navidrome_accounts WHERE user_id=?", (user_id,)
            ).fetchone()
        return (str(row["username"]), self._box.decrypt(row["password_ct"])) if row else None

    def delete_account(self, user_id: str) -> bool:
        """Forget the login and the mirror links (the playlists stay in Navidrome)."""
        with self._lock:
            cur = self._conn.execute("DELETE FROM navidrome_accounts WHERE user_id=?", (user_id,))
            self._conn.execute("DELETE FROM navidrome_playlists WHERE user_id=?", (user_id,))
        return cur.rowcount > 0

    def account_users(self) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT user_id FROM navidrome_accounts ORDER BY user_id"
            ).fetchall()
        return [str(r[0]) for r in rows]

    # --- mirrored playlists ---
    def link(self, user_id: str, spotify_id: str, name: str) -> None:
        """Mirror a playlist (again), keeping its Navidrome id if it had one."""
        with self._lock:
            self._conn.execute(
                "INSERT INTO navidrome_playlists(user_id, spotify_id, name, enabled, created_at) "
                "VALUES (?, ?, ?, 1, ?) ON CONFLICT(user_id, spotify_id) DO UPDATE SET "
                "name=excluded.name, enabled=1",
                (user_id, spotify_id, name, now_iso()),
            )

    def set_enabled(self, user_id: str, spotify_id: str, on: bool) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE navidrome_playlists SET enabled=? WHERE user_id=? AND spotify_id=?",
                (int(on), user_id, spotify_id),
            )
        return cur.rowcount > 0

    def links(self, user_id: str, *, enabled_only: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM navidrome_playlists WHERE user_id=?"
        if enabled_only:
            sql += " AND enabled=1"
        with self._lock:
            rows = self._conn.execute(sql + " ORDER BY created_at, name", (user_id,)).fetchall()
        return [dict(r) | {"enabled": bool(r["enabled"])} for r in rows]

    def record_push(
        self,
        user_id: str,
        spotify_id: str,
        *,
        navidrome_id: str | None = None,
        matched: int = 0,
        in_library: int = 0,
        total: int = 0,
        error: str | None = None,
    ) -> None:
        """A push's outcome. A failed push (``error``) keeps the last good numbers."""
        with self._lock:
            if error is not None:
                self._conn.execute(
                    "UPDATE navidrome_playlists SET error=? WHERE user_id=? AND spotify_id=?",
                    (error, user_id, spotify_id),
                )
                return
            self._conn.execute(
                "UPDATE navidrome_playlists SET navidrome_id=?, matched=?, in_library=?, "
                "total=?, last_pushed_at=?, error=NULL WHERE user_id=? AND spotify_id=?",
                (navidrome_id, matched, in_library, total, now_iso(), user_id, spotify_id),
            )

    # --- song id cache ---
    def song_ids(self, keys: Iterable[str], *, fresh_after: str) -> dict[str, str]:
        """Cached song ids for ``keys`` checked at or after ``fresh_after``."""
        wanted = sorted(set(keys))
        out: dict[str, str] = {}
        with self._lock:
            for i in range(0, len(wanted), _CHUNK):
                chunk = wanted[i : i + _CHUNK]
                marks = ",".join("?" * len(chunk))  # only "?" marks are interpolated
                rows = self._conn.execute(
                    f"SELECT merge_key, song_id FROM navidrome_songs "
                    f"WHERE merge_key IN ({marks}) AND checked_at >= ?",
                    (*chunk, fresh_after),
                ).fetchall()
                out.update({str(r[0]): str(r[1]) for r in rows})
        return out

    def put_song(self, key: str, song_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO navidrome_songs(merge_key, song_id, checked_at) VALUES (?, ?, ?) "
                "ON CONFLICT(merge_key) DO UPDATE SET song_id=excluded.song_id, "
                "checked_at=excluded.checked_at",
                (key, song_id, now_iso()),
            )
