"""Phase D: playlist-sync pure core — track_key, diff_playlist, and sync_tick
driven against in-memory fakes (no DB, no network)."""

from __future__ import annotations

from typing import Any

from crate.application.playlist_sync import diff_playlist, sync_tick, track_key


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


# --- sync_tick ---
class FakeSyncStore:
    """Minimal SyncedPlaylistStore: playlist rows + per-playlist ledger sets."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.ledger: dict[str, set[str]] = {r["id"]: set() for r in rows}
        self.checked: list[tuple[str, str | None]] = []

    def list_synced_playlists(
        self, *, active_only: bool = False, user_id: str | None = None
    ) -> list[dict[str, Any]]:
        return [r for r in self.rows if not active_only or r["active"]]

    def synced_track_keys(self, sid: str) -> set[str]:
        return set(self.ledger[sid])

    def record_synced_tracks(self, sid: str, keys: Any) -> None:
        self.ledger[sid].update(keys)

    def mark_synced_checked(self, sid: str, snapshot_id: str | None) -> None:
        self.checked.append((sid, snapshot_id))
        for r in self.rows:
            if r["id"] == sid:
                r["snapshot_id"] = snapshot_id


class FakeSource:
    def __init__(self, snapshot: str | None, tracks: list[dict[str, Any]]) -> None:
        self._snapshot = snapshot
        self._tracks = tracks
        self.track_calls = 0

    def playlist_snapshot(self, user_id: str, ref: str) -> str | None:
        return self._snapshot

    def playlist_tracks(
        self, user_id: str, ref: str, max_tracks: int = 2000
    ) -> list[dict[str, Any]]:
        self.track_calls += 1
        return self._tracks


def _row(active: bool = True, snapshot: str | None = None) -> dict[str, Any]:
    return {
        "id": "s1",
        "user_id": "u1",
        "spotify_ref": "p1",
        "name": "Mix",
        "active": active,
        "snapshot_id": snapshot,
    }


def test_first_poll_seeds_ledger_without_enqueue() -> None:
    store = FakeSyncStore([_row(snapshot=None)])
    source = FakeSource("snapA", [_t(isrc="US1"), _t(isrc="US2")])
    queued: list[tuple[dict[str, Any], str]] = []
    n = sync_tick(store, source, lambda tr, uid: queued.append((tr, uid)))
    assert n == 0 and queued == []  # baseline is not downloaded
    assert store.ledger["s1"] == {"isrc:US1", "isrc:US2"}  # but it's remembered
    assert store.rows[0]["snapshot_id"] == "snapA"


def test_later_poll_enqueues_only_new() -> None:
    store = FakeSyncStore([_row(snapshot="snapA")])
    store.ledger["s1"] = {"isrc:US1"}  # US1 already seen
    source = FakeSource("snapB", [_t(isrc="US1"), _t(isrc="US2", query="new")])
    queued: list[tuple[dict[str, Any], str]] = []
    n = sync_tick(store, source, lambda tr, uid: queued.append((tr, uid)))
    assert n == 1
    assert [q[0]["isrc"] for q in queued] == ["US2"]
    assert queued[0][1] == "u1"  # attributed to the subscriber
    assert "isrc:US2" in store.ledger["s1"]


def test_unchanged_snapshot_short_circuits() -> None:
    store = FakeSyncStore([_row(snapshot="snapA")])
    source = FakeSource("snapA", [_t(isrc="US1")])  # same snapshot
    n = sync_tick(store, source, lambda tr, uid: None)
    assert n == 0
    assert source.track_calls == 0  # never pulled the track list
    assert store.checked == [("s1", "snapA")]  # but the clock was touched


def test_inactive_playlist_is_skipped() -> None:
    store = FakeSyncStore([_row(active=False, snapshot="snapA")])
    source = FakeSource("snapB", [_t(isrc="US1")])
    n = sync_tick(store, source, lambda tr, uid: None)
    assert n == 0 and source.track_calls == 0


def test_second_tick_is_idempotent() -> None:
    store = FakeSyncStore([_row(snapshot="snapA")])
    store.ledger["s1"] = {"isrc:US1"}
    source = FakeSource("snapB", [_t(isrc="US1"), _t(isrc="US2")])
    calls: list[str] = []
    enqueue = lambda tr, uid: calls.append(tr["isrc"])  # noqa: E731
    assert sync_tick(store, source, enqueue) == 1  # US2 queued
    # snapshot advanced to snapB; a re-run with the same snapshot short-circuits
    assert sync_tick(store, source, enqueue) == 0
    assert calls == ["US2"]  # never re-queued


def test_one_bad_playlist_does_not_stop_the_sweep() -> None:
    rows = [_row(snapshot="old"), {**_row(snapshot="old"), "id": "s2", "spotify_ref": "p2"}]
    store = FakeSyncStore(rows)
    store.ledger["s1"] = set()
    store.ledger["s2"] = {"isrc:US1"}

    class Boom(FakeSource):
        def playlist_snapshot(self, user_id: str, ref: str) -> str | None:
            if ref == "p1":
                raise RuntimeError("token expired")
            return "snapB"

    source = Boom("snapB", [_t(isrc="US1"), _t(isrc="US2")])
    seen: list[str] = []
    n = sync_tick(store, source, lambda tr, uid: seen.append(tr["isrc"]))
    assert n == 1 and seen == ["US2"]  # p2 still processed after p1 blew up
