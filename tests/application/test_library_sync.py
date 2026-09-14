"""library_sync: track keys, playlist sync-state, affinity tiers, auto-sync
enqueue (and its dry run). Real SQLite stores, a fake Spotify reader."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from crate.application.affinity import TrackPlays
from crate.application.auth import SpotifyProfile, User
from crate.application.jobs import JobCancelled, JobContext
from crate.application.library_sync import (
    LIKED_ID,
    LibraryIndex,
    SyncDeps,
    content_snapshot,
    diff_playlist,
    effective_mode,
    run_library_sync,
    track_key,
)
from crate.domain.affinity import AffinityTier
from crate.domain.jobs import JobKind
from crate.domain.listening import merge_key
from crate.infrastructure.sqlite.affinity_store import SqliteAffinityStore
from crate.infrastructure.sqlite.db import connect
from crate.infrastructure.sqlite.job_store import SqliteJobStore
from crate.infrastructure.sqlite.listening_store import SqliteListeningStore
from crate.infrastructure.sqlite.store import RequestStore
from crate.infrastructure.sqlite.sync_store import SqlitePlaylistSyncStore, SqliteSyncQueue
from crate.infrastructure.sqlite.user_store import SqliteUserRepository


def _t(artist: str = "", title: str = "", isrc: str = "", query: str = "q") -> dict[str, Any]:
    return {"artist": artist, "title": title, "isrc": isrc, "query": query}


# --- track_key ---
def test_track_key_prefers_isrc() -> None:
    assert track_key(_t(artist="A", title="T", isrc="us1234567890")) == "isrc:US1234567890"


def test_track_key_falls_back_to_tokens() -> None:
    key = track_key(_t(artist="Daft Punk", title="Get Lucky"))
    assert key.startswith("tok:") and "lucky" in key


def test_track_key_empty_is_bare_tok() -> None:
    assert track_key(_t()) == "tok:"


# --- diff_playlist ---
def test_diff_returns_only_unknown() -> None:
    known = {track_key(_t(isrc="US1"))}
    cur = [_t(isrc="US1"), _t(isrc="US2"), _t(isrc="US3")]
    new = diff_playlist(known, cur)
    assert [t["isrc"] for t in new] == ["US2", "US3"]


def test_diff_dedupes_within_snapshot_and_drops_empty() -> None:
    cur = [_t(isrc="US2"), _t(isrc="US2"), _t()]  # dup + an uninformative row
    new = diff_playlist(set(), cur)
    assert [t["isrc"] for t in new] == ["US2"]


def test_diff_isrc_and_token_keys_coexist() -> None:
    known = {track_key(_t(artist="A", title="T"))}  # token key
    cur = [_t(artist="A", title="T"), _t(isrc="US9")]
    new = diff_playlist(known, cur)
    assert [track_key(t) for t in new] == ["isrc:US9"]


NOW = datetime(2026, 9, 14, tzinfo=UTC)


def trk(artist: str, title: str, isrc: str | None = None) -> dict[str, Any]:
    return {
        "id": f"id-{title}",
        "artist": artist,
        "title": title,
        "isrc": isrc,
        "query": f"{artist} {title}",
    }


class FakeReader:
    def __init__(self) -> None:
        self.playlists: list[dict[str, Any]] = [
            {"id": "p1", "name": "Mine", "image": None, "snapshot_id": "s1", "owner_id": "sp"},
            {
                "id": "dw",
                "name": "Discover Weekly",
                "image": None,
                "snapshot_id": "w1",
                "owner_id": "spotify",
            },
            {
                "id": "f1",
                "name": "Followed",
                "image": None,
                "snapshot_id": "a1",
                "owner_id": "friend",
            },
        ]
        self.tracks = {
            "p1": [trk("A", "One", "isrc1"), trk("B", "Two"), trk("B", "Two")],
            "dw": [trk("C", "Three")],
            "f1": [trk("A", "One", "ISRC1")],
        }
        self.liked = [trk("D", "Four")]
        self.top: dict[str, list[dict[str, Any]]] = {}
        self.missing: list[str] = []
        self.fetches: list[str] = []
        self.fail_for: set[str] = set()
        self.broken: set[str] = set()
        self.offline: set[str] = set()

    def connected(self, user_id: str) -> bool:
        return user_id not in self.offline

    def missing_scopes(self, user_id: str) -> list[str]:
        return list(self.missing)

    def my_playlists(self, user_id: str) -> list[dict[str, Any]]:
        if user_id in self.fail_for:
            raise RuntimeError("token revoked")
        return [dict(p) for p in self.playlists]

    def liked_songs(self, user_id: str, max_tracks: int = 2000) -> list[dict[str, Any]]:
        return list(self.liked)

    def playlist_tracks(
        self, user_id: str, playlist_id: str, max_tracks: int = 2000
    ) -> list[dict[str, Any]]:
        if playlist_id in self.broken:
            raise RuntimeError("404")
        self.fetches.append(playlist_id)
        return list(self.tracks[playlist_id])

    def top_tracks(self, user_id: str, time_range: str, limit: int = 100) -> list[dict[str, Any]]:
        return list(self.top.get(time_range, []))


class FakeHistory:
    """An imported export (the exact-tier path)."""

    def __init__(self, plays: list[TrackPlays]) -> None:
        self._plays = plays

    def history_status(self, user_id: str) -> dict[str, Any]:
        return {"export": {"start": "2020-01-01T00:00:00Z", "end": "2026-01-01T00:00:00Z"}}

    def track_plays(self, user_id: str) -> list[TrackPlays]:
        return self._plays


class World:
    def __init__(self, tmp_path: Path) -> None:
        self.conn = connect(tmp_path / "crate.db")
        self.users = SqliteUserRepository(self.conn)
        self.user: User = self.users.upsert_spotify_user(SpotifyProfile("sp", "Sai"))
        self.reader = FakeReader()
        self.listening = SqliteListeningStore(self.conn)
        self.affinity = SqliteAffinityStore(self.conn)
        self.playlists = SqlitePlaylistSyncStore(self.conn)
        self.jobs = SqliteJobStore(self.conn)
        self.requests = RequestStore(self.conn)
        self.queue = SqliteSyncQueue(self.conn, self.requests)
        self.index = LibraryIndex(frozenset({"ISRC1"}), frozenset())
        self.jobs.enqueue(JobKind.LIBRARY_SYNC, triggered_by="manual")
        job = self.jobs.claim_next("sync")
        assert job is not None
        self.job = job  # one claimed job; every run in a test logs to it

    def run(
        self,
        users: list[User] | None = None,
        history: Any = None,
        *,
        live: bool = False,
        cap: int = 300,
    ) -> dict[str, Any]:
        deps = SyncDeps(
            self.reader,
            history or self.listening,
            self.affinity,
            self.playlists,
            self.index,
            self.queue if live else None,
        )
        return run_library_sync(
            JobContext(self.job, self.jobs),
            users or [self.user],
            deps,
            now=NOW,
            max_enqueue=cap,
        )

    def rows(self, include_removed: bool = False) -> dict[str, dict[str, Any]]:
        return {
            p["spotify_id"]: p
            for p in self.playlists.playlists(self.user.id, include_removed=include_removed)
        }

    def events(self, code: str) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in self.conn.execute(
                "SELECT level, code, message, user_id FROM job_events WHERE job_id=? AND code=?",
                (self.job.id, code),
            )
        ]


def test_content_snapshot_is_stable_and_order_sensitive() -> None:
    a, b = trk("A", "One"), trk("B", "Two")
    assert content_snapshot([a, b]) == content_snapshot([dict(a), dict(b)])
    assert content_snapshot([a, b]) != content_snapshot([b, a])
    assert content_snapshot([]).startswith("c:")


@pytest.mark.parametrize(
    ("mode", "generated", "tier1", "want"),
    [
        ("on", True, False, True),
        ("off", False, True, False),
        ("inherit", False, True, True),
        ("inherit", False, False, False),
        ("inherit", True, True, False),  # Spotify-made playlists don't inherit
    ],
)
def test_effective_mode(mode: str, generated: bool, tier1: bool, want: bool) -> None:
    assert effective_mode(mode, generated=generated, tier1_auto=tier1) is want


def test_library_index_matches_isrc_or_merge_key() -> None:
    index = LibraryIndex(frozenset({"ABC"}), frozenset({merge_key("Daft Punk", "Get Lucky")}))
    assert index.has_track({"isrc": " abc "})
    assert index.has_track({"artist": "Daft Punk, Pharrell", "title": "Get Lucky - Remastered"})
    assert not index.has_track({"artist": "Daft Punk", "title": "Around the World"})
    assert index.has_key(merge_key("Daft Punk", "Get Lucky")) and not index.has_key("tok:x")


def test_first_run_records_playlists_flags_and_library_counts(tmp_path: Path) -> None:
    w = World(tmp_path)
    summary = w.run()
    rows = w.rows()
    assert list(rows) == [LIKED_ID, "p1", "f1", "dw"] or set(rows) == {LIKED_ID, "p1", "f1", "dw"}
    assert rows[LIKED_ID]["owned"] and rows["p1"]["owned"] and not rows["f1"]["owned"]
    assert rows["dw"]["generated"] and not rows["p1"]["generated"]
    # the duplicate "Two" collapses; ISRC matching ignores case
    assert (rows["p1"]["track_count"], rows["p1"]["in_library_count"]) == (2, 1)
    assert rows["f1"]["in_library_count"] == 1 and rows[LIKED_ID]["track_count"] == 1
    assert w.reader.fetches == ["p1", "dw", "f1"]
    assert summary["playlists"] == 4 and summary["playlists_changed"] == 4
    assert summary["tier1_tracks"] == 3 and summary["tier1_in_library"] == 1  # DW excluded
    assert summary["dry_run"] is True and summary["would_enqueue"] == 0  # nothing auto-synced


def test_unchanged_snapshots_are_not_refetched(tmp_path: Path) -> None:
    w = World(tmp_path)
    w.run()
    w.reader.fetches.clear()
    summary = w.run()
    assert w.reader.fetches == [] and summary["playlists_changed"] == 0
    assert [t["title"] for t in w.playlists.items(w.user.id, "p1")] == ["One", "Two", "Two"]
    assert w.rows()["p1"]["in_library_count"] == 1  # still recounted from stored items


def test_changed_removed_and_refollowed_playlists(tmp_path: Path) -> None:
    w = World(tmp_path)
    w.run()
    w.reader.fetches.clear()
    w.reader.playlists[0]["snapshot_id"] = "s2"
    w.reader.tracks["p1"].append(trk("E", "Five"))
    followed = w.reader.playlists.pop(2)
    summary = w.run()
    assert w.reader.fetches == ["p1"]
    assert summary["playlists_changed"] == 1 and summary["playlists_removed"] == 1
    assert "f1" not in w.rows() and w.rows(include_removed=True)["f1"]["removed_at"]
    assert w.rows()["p1"]["track_count"] == 3
    w.reader.playlists.append(followed)
    w.run()
    assert w.rows()["f1"]["removed_at"] is None  # back, with its stored items
    assert w.rows()["f1"]["in_library_count"] == 1


def test_dry_run_counts_what_auto_sync_would_queue(tmp_path: Path) -> None:
    w = World(tmp_path)
    w.run()
    w.affinity.set_auto_sync(w.user.id, AffinityTier.CORE, True)
    summary = w.run()
    assert summary["would_enqueue"] == 2  # Two + Four; One is in the library; DW stays off
    event = w.events("would_enqueue")[-1]
    assert event["user_id"] == w.user.id  # personal -> scoped to the user
    assert w.playlists.set_sync_mode(w.user.id, "dw", "on")
    assert w.run()["would_enqueue"] == 3
    assert w.playlists.set_sync_mode(w.user.id, "p1", "off")
    assert w.run()["would_enqueue"] == 2  # Four + Three


def test_approximate_tiers_from_top_lists(tmp_path: Path) -> None:
    w = World(tmp_path)
    w.reader.top = {
        "long_term": [trk("A", "Evergreen")],
        "medium_term": [trk("A", "Evergreen"), trk("B", "Summer")],
        "short_term": [],
    }
    summary = w.run()
    assert (summary["constants"], summary["flings"], summary["approximate_users"]) == (1, 1, 1)
    assert w.affinity.members(w.user.id, AffinityTier.CONSTANT)[0]["approximate"] is True
    snaps = w.conn.execute("SELECT COUNT(*) FROM top_snapshots").fetchone()[0]
    assert snaps == 3
    assert all(e["user_id"] == w.user.id for e in w.events("tier"))
    w.affinity.set_auto_sync(w.user.id, AffinityTier.CONSTANT, True)
    assert w.run()["would_enqueue"] == 1  # Evergreen


def test_missing_top_scope_skips_tiers_with_a_relogin_hint(tmp_path: Path) -> None:
    w = World(tmp_path)
    w.reader.missing = ["user-top-read"]
    summary = w.run()
    assert summary["constants"] == 0 and summary["approximate_users"] == 0
    assert w.events("relogin")[0]["user_id"] == w.user.id


def test_export_gives_exact_tiers(tmp_path: Path) -> None:
    w = World(tmp_path)
    times = [
        (datetime(2022, 1 + 3 * q, 1, tzinfo=UTC) + timedelta(days=5 * i)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        for q in range(4)
        for i in range(4)
    ]
    steady = TrackPlays("tok:a steady", times, "A", "Steady", "spotify:track:s1")
    summary = w.run(history=FakeHistory([steady]))
    assert summary["constants"] == 1 and summary["approximate_users"] == 0
    assert w.conn.execute("SELECT COUNT(*) FROM top_snapshots").fetchone()[0] == 0


def test_failures_are_isolated_per_user_and_playlist(tmp_path: Path) -> None:
    w = World(tmp_path)
    friend = w.users.upsert_spotify_user(SpotifyProfile("sp2", "Friend"))
    ghost = w.users.upsert_spotify_user(SpotifyProfile("sp3", "Ghost"))
    w.reader.fail_for = {w.user.id}
    w.reader.offline = {ghost.id}
    w.reader.broken = {"f1"}
    summary = w.run(users=[w.user, friend, ghost])
    assert (summary["users"], summary["failed"], summary["skipped"]) == (1, 1, 1)
    assert summary["playlist_errors"] == 1  # f1 for the friend; the rest still synced
    assert w.events("user_failed") and w.events("playlist_failed")[0]["user_id"] == friend.id


def test_cancel_stops_the_run(tmp_path: Path) -> None:
    w = World(tmp_path)
    w.jobs.request_cancel(w.job.id)
    with pytest.raises(JobCancelled):
        w.run()


def test_sync_mode_validation(tmp_path: Path) -> None:
    w = World(tmp_path)
    w.run()
    assert w.playlists.set_sync_mode(w.user.id, "nope", "on") is False
    with pytest.raises(ValueError, match="sync mode"):
        w.playlists.set_sync_mode(w.user.id, "p1", "sometimes")


def test_library_index_from_completed_requests(tmp_path: Path) -> None:
    from crate.domain.identity import TrackIdentity
    from crate.infrastructure.sqlite.store import RequestStore

    store = RequestStore(connect(tmp_path / "crate.db"))
    done = store.enqueue("A One", identity=TrackIdentity(artist="A", title="One", isrc="x1"))
    store.enqueue("B Two", identity=TrackIdentity(artist="B", title="Two"))  # still queued
    store._conn.execute("UPDATE requests SET status='done' WHERE id=?", (done,))
    isrcs, keys = store.library_index()
    assert isrcs == {"X1"} and keys == {merge_key("A", "One")}
    # files the dedup scan found on disk count too: by tags, and by AcoustID's id
    now = "2026-09-14T00:00:00+00:00"
    for fid, tag_key, aid, missing in (
        ("f1", merge_key("C", "Three"), None, None),
        ("f2", None, ("D", "Four"), None),
        ("f3", merge_key("E", "Gone"), None, now),  # trashed / deleted: not in the library
    ):
        store._conn.execute(
            "INSERT INTO library_files(id, path, size, mtime_ns, tag_key, aid_artist, aid_title, "
            "first_seen_at, scanned_at, missing_since) VALUES (?, ?, 1, 1, ?, ?, ?, ?, ?, ?)",
            (fid, f"{fid}.flac", tag_key, *(aid or (None, None)), now, now, missing),
        )
    _, keys = store.library_index()
    assert keys == {merge_key("A", "One"), merge_key("C", "Three"), merge_key("D", "Four")}
    assert track_key({"isrc": "x1"}) == "isrc:X1" and diff_playlist(set(), []) == []


def _sync_rows(w: World) -> list[dict[str, Any]]:
    return [
        dict(r)
        for r in w.conn.execute(
            "SELECT r.query, r.priority, r.user_id, b.origin, b.name FROM requests r "
            "JOIN batches b ON b.id = r.batch_id ORDER BY b.name"
        )
    ]


def test_live_run_queues_missing_tracks_once_at_sync_priority(tmp_path: Path) -> None:
    w = World(tmp_path)
    w.affinity.set_auto_sync(w.user.id, AffinityTier.CORE, True)
    s = w.run(live=True)
    assert s["dry_run"] is False and (s["enqueued"], s["deferred"]) == (2, 0)
    rows = _sync_rows(w)
    assert {r["priority"] for r in rows} == {1} and {r["origin"] for r in rows} == {"sync"}
    assert {r["user_id"] for r in rows} == {w.user.id}
    assert [r["name"] for r in rows] == ["Auto-sync: Liked Songs", "Auto-sync: Mine"]
    assert w.events("enqueued")[0]["user_id"] == w.user.id
    assert w.run(live=True)["enqueued"] == 0  # the ledger remembers


def test_cap_defers_the_rest_to_the_next_run(tmp_path: Path) -> None:
    w = World(tmp_path)
    w.affinity.set_auto_sync(w.user.id, AffinityTier.CORE, True)
    s = w.run(live=True, cap=1)
    assert (s["enqueued"], s["deferred"]) == (1, 1)
    assert [r["name"] for r in _sync_rows(w)] == ["Auto-sync: Liked Songs"]  # tier 1 first
    assert w.events("deferred")
    assert w.run(live=True, cap=1)["enqueued"] == 1
    assert w.run(live=True, cap=1)["enqueued"] == 0


def test_cap_zero_is_a_dry_run_even_with_a_queue(tmp_path: Path) -> None:
    w = World(tmp_path)
    w.affinity.set_auto_sync(w.user.id, AffinityTier.CORE, True)
    s = w.run(live=True, cap=0)
    assert s["dry_run"] is True and s["would_enqueue"] == 2 and _sync_rows(w) == []


def test_a_song_is_queued_once_across_scopes_and_baselines_hold(tmp_path: Path) -> None:
    w = World(tmp_path)
    w.reader.tracks["dw"].append(trk("B", "Two"))
    w.run()  # learn the playlists
    w.playlists.set_sync_mode(w.user.id, "p1", "on")
    assert w.run(live=True)["enqueued"] == 1  # Two, via Mine
    w.playlists.set_sync_mode(w.user.id, "dw", "on")
    assert w.run(live=True)["enqueued"] == 1  # only Three; Two is already queued
    assert w.queue.baseline(w.user.id, "playlist:liked", [merge_key("D", "Four")]) == 1
    assert w.queue.baseline(w.user.id, "playlist:liked", [merge_key("D", "Four")]) == 0
    w.playlists.set_sync_mode(w.user.id, LIKED_ID, "on")
    assert w.run(live=True)["enqueued"] == 0  # Four was baselined


def test_tier_auto_sync_queues_members_with_a_spotify_id(tmp_path: Path) -> None:
    w = World(tmp_path)
    w.reader.top = {
        "long_term": [trk("A", "Evergreen")],
        "medium_term": [trk("A", "Evergreen")],
    }
    w.affinity.set_auto_sync(w.user.id, AffinityTier.CONSTANT, True)
    assert w.run(live=True)["enqueued"] == 1
    assert [r["name"] for r in _sync_rows(w)] == ["Auto-sync: Long-term constants"]


def test_claim_prefers_the_users_own_requests_over_sync(tmp_path: Path) -> None:
    store = RequestStore(connect(tmp_path / "crate.db"))
    sync_id = store.enqueue("sync one", priority=1)
    manual_id = store.enqueue("manual one")
    first, second = store.claim_next(), store.claim_next()
    assert first is not None and second is not None
    assert (first["id"], second["id"]) == (manual_id, sync_id)
