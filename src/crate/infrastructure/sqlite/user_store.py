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
    SERVICE_USER_ID,
    SESSION_TTL_S,
    SpotifyProfile,
    User,
    hash_token,
)
from crate.domain.oidc import OidcProfile
from crate.infrastructure.sqlite.db import now_iso

_USER_COLS = "id, spotify_id, display_name, email, avatar_url, is_admin, oidc_sub"


def _user(row: sqlite3.Row) -> User:
    return User(
        id=row["id"],
        spotify_id=row["spotify_id"],
        display_name=row["display_name"],
        email=row["email"],
        avatar_url=row["avatar_url"],
        is_admin=bool(row["is_admin"]),
        oidc_sub=row["oidc_sub"],
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

    def list_users(self, *, include_service: bool = False) -> list[User]:
        """Every app user, oldest first (the synthetic service user only on request)."""
        sql = f"SELECT {_USER_COLS} FROM users"
        params: tuple[str, ...] = ()
        if not include_service:
            sql += " WHERE id != ?"
            params = (SERVICE_USER_ID,)
        with self._lock:
            rows = self._conn.execute(sql + " ORDER BY created_at", params).fetchall()
        return [_user(r) for r in rows]

    def set_admin(self, user_id: str, is_admin: bool) -> User | None:
        with self._lock:
            self._conn.execute(
                "UPDATE users SET is_admin=? WHERE id=?", (1 if is_admin else 0, user_id)
            )
            row = self._conn.execute(
                f"SELECT {_USER_COLS} FROM users WHERE id=?", (user_id,)
            ).fetchone()
        return _user(row) if row else None

    # --- SSO (ADR-0023) ---
    def upsert_oidc_user(self, profile: OidcProfile) -> User:
        """Insert or update the user for ``(issuer, sub)``. A provider without a
        picture keeps the avatar a linked Spotify account supplied."""
        with self._lock:
            now = now_iso()
            self._conn.execute(
                "INSERT INTO users(id, oidc_issuer, oidc_sub, display_name, email, avatar_url, "
                "created_at, last_seen_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(oidc_issuer, oidc_sub) DO UPDATE SET "
                "display_name=excluded.display_name, email=excluded.email, "
                "avatar_url=COALESCE(excluded.avatar_url, users.avatar_url), "
                "last_seen_at=excluded.last_seen_at",
                (
                    uuid.uuid4().hex,
                    profile.issuer,
                    profile.sub,
                    profile.display_name,
                    profile.email,
                    profile.avatar_url,
                    now,
                    now,
                ),
            )
            row = self._conn.execute(
                f"SELECT {_USER_COLS} FROM users WHERE oidc_issuer=? AND oidc_sub=?",
                (profile.issuer, profile.sub),
            ).fetchone()
        return _user(row)

    def link_spotify(self, user_id: str, profile: SpotifyProfile) -> User:
        with self._lock:
            self._conn.execute(
                "UPDATE users SET spotify_id=?, avatar_url=COALESCE(avatar_url, ?) WHERE id=?",
                (profile.spotify_id, profile.avatar_url, user_id),
            )
            row = self._conn.execute(
                f"SELECT {_USER_COLS} FROM users WHERE id=?", (user_id,)
            ).fetchone()
        return _user(row)

    def unlink_spotify(self, user_id: str) -> None:
        with self._lock:
            self._conn.execute("UPDATE users SET spotify_id=NULL WHERE id=?", (user_id,))

    def _user_tables(self) -> list[str]:
        """Every table with a ``user_id`` column (found at run time, so tables added
        by later migrations are covered too)."""
        names = [
            str(r[0])
            for r in self._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT IN ('users', 'sqlite_sequence', 'schema_migrations')"
            )
        ]
        return [
            n
            for n in names
            if any(c[1] == "user_id" for c in self._conn.execute(f'PRAGMA table_info("{n}")'))
        ]

    def merge_into(self, src_id: str, dst_id: str) -> None:
        """Fold ``src_id`` into ``dst_id`` in one transaction: every row it owns
        moves over (``dst_id``'s own row wins where a table allows only one), its
        sessions follow, it's deleted, and ``dst_id`` takes its SSO identity and name."""
        if src_id == dst_id:
            return
        with self._lock:
            self._conn.execute("SAVEPOINT merge_users")
            try:
                src = self._conn.execute(
                    "SELECT oidc_issuer, oidc_sub, display_name, email FROM users WHERE id=?",
                    (src_id,),
                ).fetchone()
                if src is None:
                    raise ValueError(f"no user {src_id!r}")
                for table in self._user_tables():
                    self._conn.execute(
                        f'UPDATE OR IGNORE "{table}" SET user_id=? WHERE user_id=?',
                        (dst_id, src_id),
                    )
                    self._conn.execute(f'DELETE FROM "{table}" WHERE user_id=?', (src_id,))
                self._conn.execute("DELETE FROM users WHERE id=?", (src_id,))
                if src["oidc_sub"] is not None:
                    self._conn.execute(
                        "UPDATE users SET oidc_issuer=?, oidc_sub=?, display_name=?, "
                        "email=COALESCE(?, email) WHERE id=?",
                        (
                            src["oidc_issuer"],
                            src["oidc_sub"],
                            src["display_name"],
                            src["email"],
                            dst_id,
                        ),
                    )
            except BaseException:
                self._conn.execute("ROLLBACK TO merge_users")
                self._conn.execute("RELEASE merge_users")
                raise
            self._conn.execute("RELEASE merge_users")


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
