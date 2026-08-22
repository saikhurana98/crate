"""SQLite users + sessions (Phase 2 multi-user): upsert idempotency, session
lifecycle, hashed-at-rest tokens, expiry."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from crate.application.auth import SpotifyProfile, hash_token
from crate.infrastructure.sqlite.db import connect
from crate.infrastructure.sqlite.user_store import SqliteSessionStore, SqliteUserRepository


def _profile(sid: str = "spot1", name: str = "Sai") -> SpotifyProfile:
    return SpotifyProfile(spotify_id=sid, display_name=name, email="s@x.io", avatar_url="http://a")


def test_upsert_is_idempotent_and_updates_fields(tmp_path: Path) -> None:
    repo = SqliteUserRepository(connect(tmp_path / "u.db"))
    first = repo.upsert_spotify_user(_profile(name="Old"))
    second = repo.upsert_spotify_user(_profile(name="New Name"))
    # same spotify_id -> same app id, fields updated in place
    assert first.id == second.id
    assert second.display_name == "New Name"
    assert repo.by_spotify_id("spot1") == second
    assert repo.get(first.id) == second


def test_service_user_seeded_by_migration(tmp_path: Path) -> None:
    repo = SqliteUserRepository(connect(tmp_path / "u.db"))
    svc = repo.get("service")
    assert svc is not None and svc.spotify_id is None


def test_get_and_by_spotify_id_miss_returns_none(tmp_path: Path) -> None:
    repo = SqliteUserRepository(connect(tmp_path / "u.db"))
    assert repo.get("nope") is None
    assert repo.by_spotify_id("nope") is None


def test_session_create_resolve_revoke(tmp_path: Path) -> None:
    conn = connect(tmp_path / "u.db")
    repo = SqliteUserRepository(conn)
    user = repo.upsert_spotify_user(_profile())
    sessions = SqliteSessionStore(conn)

    raw = sessions.create(user.id)
    assert sessions.resolve(raw) == user.id
    # stored hashed, never raw
    row = conn.execute("SELECT token_hash FROM sessions").fetchone()
    assert row["token_hash"] == hash_token(raw) and row["token_hash"] != raw

    sessions.revoke(raw)
    assert sessions.resolve(raw) is None
    assert sessions.resolve("") is None


def test_session_expiry_and_sweep(tmp_path: Path) -> None:
    conn = connect(tmp_path / "u.db")
    repo = SqliteUserRepository(conn)
    user = repo.upsert_spotify_user(_profile())

    clock = {"t": datetime(2026, 1, 1, tzinfo=UTC)}
    sessions = SqliteSessionStore(conn, now=lambda: clock["t"])
    raw = sessions.create(user.id, ttl_s=60)
    assert sessions.resolve(raw) == user.id

    clock["t"] = clock["t"] + timedelta(seconds=61)  # past expiry
    assert sessions.resolve(raw) is None
    assert sessions.sweep_expired() == 1
    assert conn.execute("SELECT COUNT(*) c FROM sessions").fetchone()["c"] == 0
