"""SQLite-backed users + sessions (Phase 2 multi-user).

Same connection/lock discipline as :class:`RequestStore`: one WAL connection,
every method guarded by ``self._lock`` because the console's uvicorn threadpool
shares it. Session tokens are ``secrets.token_urlsafe(32)`` and only their
sha256 is stored — the raw token exists solely in the browser cookie.
"""

from __future__ import annotations

import secrets
import sqlite3
import threading
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from crate.application.auth import (
    SESSION_TTL_S,
    SpotifyProfile,
    User,
    hash_token,
)
from crate.infrastructure.sqlite.db import now_iso

_USER_COLS = "id, spotify_id, display_name, email, avatar_url, is_admin"


def _user(row: sqlite3.Row) -> User:
    return User(
        id=row["id"],
        spotify_id=row["spotify_id"],
        display_name=row["display_name"],
        email=row["email"],
        avatar_url=row["avatar_url"],
        is_admin=bool(row["is_admin"]),
    )


class SqliteUserRepository:
    def __init__(self, conn: sqlite3.Connection, *, lock: threading.Lock | None = None) -> None:
        self._conn = conn
        self._lock = lock or threading.Lock()

    def upsert_spotify_user(self, profile: SpotifyProfile) -> User:
        with self._lock:
            now = now_iso()
            self._conn.execute(
                "INSERT INTO users"
                "(id, spotify_id, display_name, email, avatar_url, created_at, last_seen_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(spotify_id) DO UPDATE SET "
                "display_name=excluded.display_name, email=excluded.email, "
                "avatar_url=excluded.avatar_url, last_seen_at=excluded.last_seen_at",
                (
                    uuid.uuid4().hex,
                    profile.spotify_id,
                    profile.display_name,
                    profile.email,
                    profile.avatar_url,
                    now,
                    now,
                ),
            )
            row = self._conn.execute(
                f"SELECT {_USER_COLS} FROM users WHERE spotify_id=?", (profile.spotify_id,)
            ).fetchone()
        return _user(row)

    def get(self, user_id: str) -> User | None:
        with self._lock:
            row = self._conn.execute(
                f"SELECT {_USER_COLS} FROM users WHERE id=?", (user_id,)
            ).fetchone()
        return _user(row) if row else None

    def by_spotify_id(self, spotify_id: str) -> User | None:
        with self._lock:
            row = self._conn.execute(
                f"SELECT {_USER_COLS} FROM users WHERE spotify_id=?", (spotify_id,)
            ).fetchone()
        return _user(row) if row else None

    def set_admin(self, user_id: str, is_admin: bool) -> User | None:
        with self._lock:
            self._conn.execute(
                "UPDATE users SET is_admin=? WHERE id=?", (1 if is_admin else 0, user_id)
            )
            row = self._conn.execute(
                f"SELECT {_USER_COLS} FROM users WHERE id=?", (user_id,)
            ).fetchone()
        return _user(row) if row else None


class SqliteSessionStore:
    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        lock: threading.Lock | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._conn = conn
        self._lock = lock or threading.Lock()
        self._now = now

    def create(self, user_id: str, ttl_s: int = SESSION_TTL_S) -> str:
        raw = secrets.token_urlsafe(32)
        now = self._now()
        expires = now + timedelta(seconds=ttl_s)
        with self._lock:
            self._conn.execute(
                "INSERT INTO sessions(token_hash, user_id, created_at, expires_at) "
                "VALUES (?, ?, ?, ?)",
                (hash_token(raw), user_id, now.isoformat(), expires.isoformat()),
            )
        return raw

    def resolve(self, raw_token: str) -> str | None:
        if not raw_token:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT user_id, expires_at FROM sessions WHERE token_hash=?",
                (hash_token(raw_token),),
            ).fetchone()
        if row is None:
            return None
        if row["expires_at"] <= self._now().isoformat():
            return None
        return str(row["user_id"])

    def revoke(self, raw_token: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM sessions WHERE token_hash=?", (hash_token(raw_token),))

    def sweep_expired(self) -> int:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM sessions WHERE expires_at <= ?", (self._now().isoformat(),)
            )
            return cur.rowcount
