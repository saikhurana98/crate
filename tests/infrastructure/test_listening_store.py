"""SqliteListeningStore on a temp DB: idempotent plays, cursors, track cache."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from crate.application.auth import SpotifyProfile
from crate.domain.listening import Play, merge_key
from crate.infrastructure.sqlite.db import connect
from crate.infrastructure.sqlite.listening_store import SqliteListeningStore
from crate.infrastructure.sqlite.user_store import SqliteUserRepository


def _setup(tmp_path: Path) -> tuple[SqliteListeningStore, str, str]:
    conn = connect(tmp_path / "crate.db")
    users = SqliteUserRepository(conn)
    a = users.upsert_spotify_user(SpotifyProfile("sa", "Sai")).id
    b = users.upsert_spotify_user(SpotifyProfile("sb", "Friend")).id
    return SqliteListeningStore(conn), a, b


def _play(at: str, title: str = "Song") -> Play:
    return Play(at, merge_key("Artist", title), "spotify:track:x", "Artist", title, "LP")


def test_add_plays_is_idempotent_and_per_user(tmp_path: Path) -> None:
    store, a, b = _setup(tmp_path)
    plays = [_play("2026-09-01T10:00:00Z"), _play("2026-09-01T10:04:00Z")]
    assert store.add_plays(a, plays, "recent") == 2
    assert store.add_plays(a, plays, "recent") == 0  # re-poll: nothing new
    assert store.add_plays(b, plays[:1], "recent") == 1  # another user's ledger
    assert store.play_count(a) == 2 and store.play_count(b) == 1
    assert store.last_played_at(a) == "2026-09-01T10:04:00Z"
    assert store.add_plays(a, [], "recent") == 0


def test_plays_for_unknown_users_are_rejected(tmp_path: Path) -> None:
    store, _, _ = _setup(tmp_path)
    with pytest.raises(sqlite3.IntegrityError):  # FK: OR IGNORE doesn't swallow it
        store.add_plays("ghost", [_play("2026-09-01T10:00:00Z")], "recent")


def test_cursor_round_trip(tmp_path: Path) -> None:
    store, a, _ = _setup(tmp_path)
    assert store.recent_cursor(a) is None and store.last_polled_at(a) is None
    store.set_recent_cursor(a, 1788000000000)
    assert store.recent_cursor(a) == 1788000000000
    store.set_recent_cursor(a, 1788000900000)  # upsert
    assert store.recent_cursor(a) == 1788000900000 and store.last_polled_at(a)


def test_track_cache_keeps_known_isrc_and_preview_id(tmp_path: Path) -> None:
    store, _, _ = _setup(tmp_path)
    store.upsert_tracks([{"id": "t1", "isrc": "ISRC1", "title": "Song", "duration_s": 200}])
    store._conn.execute("UPDATE spotify_tracks SET deezer_id='dz9' WHERE id='t1'")
    store.upsert_tracks([{"id": "t1", "isrc": None, "title": "Song (new)"}, {"title": "no id"}])
    row = store._conn.execute("SELECT * FROM spotify_tracks WHERE id='t1'").fetchone()
    assert row["isrc"] == "ISRC1" and row["title"] == "Song (new)"
    assert row["duration_s"] == 200 and row["deezer_id"] == "dz9"
    store.upsert_tracks([])  # no-op


def test_export_replaces_a_colliding_polled_play_and_reimport_is_a_noop(tmp_path: Path) -> None:
    store, a, _ = _setup(tmp_path)
    polled = _play("2021-01-01T00:00:00Z")
    assert store.add_plays(a, [polled], "recent") == 1
    exported = Play(
        polled.played_at, polled.merge_key, "spotify:track:x", "Artist", "Song", "LP", 200_000
    )
    assert store.add_plays(a, [exported], "export") == 1  # replaced, not ignored
    row = store._conn.execute(
        "SELECT source, ms_played FROM plays WHERE user_id=?", (a,)
    ).fetchone()
    assert (row["source"], row["ms_played"]) == ("export", 200_000)
    assert store.add_plays(a, [exported], "export") == 0  # re-import changes nothing
    assert store.add_plays(a, [polled], "recent") == 0  # a later poll never downgrades it
    assert store.play_count(a) == 1


def test_delete_recent_in_range_only_touches_polled_rows_in_span(tmp_path: Path) -> None:
    store, a, b = _setup(tmp_path)
    store.add_plays(a, [_play("2021-01-01T00:00:00Z", "In Span")], "recent")
    store.add_plays(a, [_play("2021-01-01T00:00:03Z", "In Span Export")], "export")
    store.add_plays(a, [_play("2025-01-01T00:00:00Z", "After")], "recent")
    store.add_plays(b, [_play("2021-01-01T00:00:00Z", "Other User")], "recent")
    assert store.delete_recent_in_range(a, "2020-01-01T00:00:00Z", "2022-01-01T00:00:00Z") == 1
    assert store.play_count(a) == 2 and store.play_count(b) == 1


def test_export_range_widens_across_uploads_and_status(tmp_path: Path) -> None:
    store, a, _ = _setup(tmp_path)
    assert store.history_status(a)["export"] is None
    store.set_export_range(a, "2020-01-01T00:00:00Z", "2021-01-01T00:00:00Z")
    store.set_export_range(a, "2019-06-01T00:00:00Z", "2020-06-01T00:00:00Z")
    store.add_plays(a, [_play("2020-02-02T00:00:00Z")], "export")
    store.add_plays(a, [_play("2026-09-01T00:00:00Z", "New")], "recent")
    status = store.history_status(a)
    assert status["export"]["start"] == "2019-06-01T00:00:00Z"
    assert status["export"]["end"] == "2021-01-01T00:00:00Z"
    assert (status["plays"], status["export_plays"], status["polled_plays"]) == (2, 1, 1)
    assert status["first_played_at"] == "2020-02-02T00:00:00Z"
