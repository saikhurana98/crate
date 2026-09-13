"""SqliteAffinityStore + listening_store.track_plays on a temp DB."""

from __future__ import annotations

from pathlib import Path

from crate.application.affinity import TierMember
from crate.application.auth import SpotifyProfile
from crate.domain.affinity import AffinityThresholds, AffinityTier
from crate.domain.listening import Play, merge_key
from crate.infrastructure.sqlite.affinity_store import SqliteAffinityStore
from crate.infrastructure.sqlite.db import connect
from crate.infrastructure.sqlite.listening_store import SqliteListeningStore
from crate.infrastructure.sqlite.user_store import SqliteUserRepository


def _setup(tmp_path: Path) -> tuple[SqliteAffinityStore, SqliteListeningStore, str]:
    conn = connect(tmp_path / "crate.db")
    uid = SqliteUserRepository(conn).upsert_spotify_user(SpotifyProfile("sp", "Sai")).id
    return SqliteAffinityStore(conn), SqliteListeningStore(conn), uid


def _m(key: str, score: float = 1.0) -> TierMember:
    return TierMember(
        AffinityTier.CONSTANT, key, f"id-{key}", "A", key, score, {"plays": int(score)}
    )


def test_thresholds_default_round_trip_and_clamp_on_read(tmp_path: Path) -> None:
    store, _, uid = _setup(tmp_path)
    assert store.thresholds(uid) == AffinityThresholds()
    store.set_thresholds(uid, AffinityThresholds(constant_min_plays=20))
    assert store.thresholds(uid).constant_min_plays == 20
    store._conn.execute(
        "UPDATE affinity_prefs SET thresholds_json=? WHERE user_id=?",
        ('{"fling_min_share": 9}', uid),
    )
    assert store.thresholds(uid).fling_min_share == 1.0  # clamped when read


def test_auto_sync_flags_default_off_and_remember_enable_time(tmp_path: Path) -> None:
    store, _, uid = _setup(tmp_path)
    assert all(not v["auto_sync"] for v in store.tier_settings(uid).values())
    store.set_auto_sync(uid, AffinityTier.CONSTANT, True)
    on = store.tier_settings(uid)[2]
    assert on["auto_sync"] is True and on["enabled_at"]
    store.set_auto_sync(uid, AffinityTier.CONSTANT, False)
    off = store.tier_settings(uid)[2]
    assert off["auto_sync"] is False and off["enabled_at"] == on["enabled_at"]


def test_apply_members_tracks_entered_left_and_rejoin(tmp_path: Path) -> None:
    store, _, uid = _setup(tmp_path)
    assert store.apply_members(uid, AffinityTier.CONSTANT, [_m("a", 5), _m("b", 9)]) == (
        ["a", "b"],
        [],
    )
    assert [m["merge_key"] for m in store.members(uid, AffinityTier.CONSTANT)] == ["b", "a"]
    assert store.apply_members(uid, AffinityTier.CONSTANT, [_m("b"), _m("c")]) == (["c"], ["a"])
    assert store.member_count(uid, AffinityTier.CONSTANT) == 2
    assert {
        m["merge_key"] for m in store.members(uid, AffinityTier.CONSTANT, include_left=True)
    } == {"a", "b", "c"}
    entered, _ = store.apply_members(uid, AffinityTier.CONSTANT, [_m("a"), _m("b"), _m("c")])
    assert entered == ["a"]  # rejoined -> counts as entering again
    row = store.members(uid, AffinityTier.CONSTANT)[0]
    assert row["stats"] and row["approximate"] is False and row["left_at"] is None


def test_members_are_per_tier(tmp_path: Path) -> None:
    store, _, uid = _setup(tmp_path)
    store.apply_members(uid, AffinityTier.FLING, [_m("x")], approximate=True)
    assert store.members(uid, AffinityTier.CONSTANT) == []
    assert store.members(uid, AffinityTier.FLING)[0]["approximate"] is True


def test_top_snapshot_is_recorded(tmp_path: Path) -> None:
    store, _, uid = _setup(tmp_path)
    store.record_top_snapshot(uid, "long_term", ["id1", "id2"])
    row = store._conn.execute(
        "SELECT ids_json FROM top_snapshots WHERE user_id=?", (uid,)
    ).fetchone()
    assert row["ids_json"] == '["id1", "id2"]'


def test_track_plays_groups_by_song_with_representative_uri(tmp_path: Path) -> None:
    _, listening, uid = _setup(tmp_path)
    key = merge_key("Artist", "Song")
    listening.add_plays(
        uid,
        [
            Play("2021-01-01T00:00:00Z", key, "spotify:track:old", "Artist", "Song"),
            Play("2021-02-01T00:00:00Z", key, "spotify:track:new", "Artist", "Song"),
            Play("2021-03-01T00:00:00Z", key, "spotify:track:new", "Artist", "Song (Remix)"),
            Play("2021-01-05T00:00:00Z", merge_key("Other", "Tune"), None, "Other", "Tune"),
        ],
        "export",
    )
    songs = {tp.merge_key: tp for tp in listening.track_plays(uid)}
    song = songs[key]
    assert song.played_at == [
        "2021-01-01T00:00:00Z",
        "2021-02-01T00:00:00Z",
        "2021-03-01T00:00:00Z",
    ]
    assert song.track_uri == "spotify:track:new"  # most-played uri
    assert song.title == "Song (Remix)"  # latest spelling
    assert songs[merge_key("Other", "Tune")].track_uri is None
