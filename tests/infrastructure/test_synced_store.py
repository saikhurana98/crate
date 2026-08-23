"""Phase D: synced_playlists + synced_tracks persistence on RequestStore."""

from __future__ import annotations

from pathlib import Path

from crate.application.auth import SpotifyProfile
from crate.infrastructure.sqlite.db import connect
from crate.infrastructure.sqlite.store import RequestStore
from crate.infrastructure.sqlite.user_store import SqliteUserRepository


def _store_with_user(tmp_path: Path) -> tuple[RequestStore, str]:
    conn = connect(tmp_path / "crate.db")
    uid = SqliteUserRepository(conn).upsert_spotify_user(SpotifyProfile("sp", "Alice")).id
    return RequestStore(conn), uid


def test_add_is_idempotent_per_user_and_playlist(tmp_path: Path) -> None:
    store, uid = _store_with_user(tmp_path)
    a = store.add_synced_playlist(uid, "p1", "Mix")
    b = store.add_synced_playlist(uid, "p1", "Mix Renamed")
    assert a == b  # same subscription
    rows = store.list_synced_playlists(user_id=uid)
    assert len(rows) == 1
    assert rows[0]["name"] == "Mix Renamed"  # name refreshed
    assert rows[0]["active"] is True
    assert rows[0]["snapshot_id"] is None  # never polled yet


def test_deactivate_and_reactivate_keeps_ledger(tmp_path: Path) -> None:
    store, uid = _store_with_user(tmp_path)
    sid = store.add_synced_playlist(uid, "p1", "Mix")
    store.record_synced_tracks(sid, ["isrc:US1", "isrc:US2"])
    assert store.set_synced_active(sid, False) is True
    assert store.list_synced_playlists(active_only=True) == []  # hidden from the sweep
    # re-subscribe: same id, active again, ledger intact (no back-catalogue re-queue)
    assert store.add_synced_playlist(uid, "p1", "Mix") == sid
    assert store.list_synced_playlists(active_only=True)[0]["id"] == sid
    assert store.synced_track_keys(sid) == {"isrc:US1", "isrc:US2"}


def test_ledger_is_idempotent(tmp_path: Path) -> None:
    store, uid = _store_with_user(tmp_path)
    sid = store.add_synced_playlist(uid, "p1", "Mix")
    store.record_synced_tracks(sid, ["isrc:US1"])
    store.record_synced_tracks(sid, ["isrc:US1", "isrc:US2"])  # US1 repeated
    assert store.synced_track_keys(sid) == {"isrc:US1", "isrc:US2"}


def test_mark_checked_stores_snapshot(tmp_path: Path) -> None:
    store, uid = _store_with_user(tmp_path)
    sid = store.add_synced_playlist(uid, "p1", "Mix")
    store.mark_synced_checked(sid, "snapA")
    row = store.list_synced_playlists(user_id=uid)[0]
    assert row["snapshot_id"] == "snapA"
    assert row["last_checked_at"] is not None


def test_list_filters_by_user(tmp_path: Path) -> None:
    store, uid = _store_with_user(tmp_path)
    other = SqliteUserRepository(store._conn).upsert_spotify_user(SpotifyProfile("sp2", "Bob")).id
    store.add_synced_playlist(uid, "p1", "Alice Mix")
    store.add_synced_playlist(other, "p2", "Bob Mix")
    mine = store.list_synced_playlists(user_id=uid)
    assert [r["spotify_ref"] for r in mine] == ["p1"]


def test_ledger_cascades_on_delete(tmp_path: Path) -> None:
    """Dropping a subscription row drops its ledger (FK ON DELETE CASCADE)."""
    store, uid = _store_with_user(tmp_path)
    sid = store.add_synced_playlist(uid, "p1", "Mix")
    store.record_synced_tracks(sid, ["isrc:US1"])
    store._conn.execute("DELETE FROM synced_playlists WHERE id=?", (sid,))
    remaining = store._conn.execute(
        "SELECT COUNT(*) FROM synced_tracks WHERE synced_playlist_id=?", (sid,)
    ).fetchone()[0]
    assert remaining == 0
