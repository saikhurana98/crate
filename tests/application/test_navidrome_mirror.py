"""Mirroring playlists into users' Navidrome accounts (fake Navidrome + store)."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

import pytest

from crate.application.jobs import Job, JobCancelled, JobContext
from crate.application.library_sync import LibraryIndex
from crate.application.navidrome_mirror import run_navidrome_push
from crate.domain.jobs import JobKind, JobStatus
from crate.domain.listening import merge_key
from crate.infrastructure.navidrome.client import NavidromeError

NOW = datetime(2026, 9, 14, tzinfo=UTC)
A = {"artist": "A", "title": "One", "isrc": "I1"}
B = {"artist": "B", "title": "Two"}
C = {"artist": "C", "title": "Three"}  # not downloaded yet
INDEX = LibraryIndex(frozenset({"I1"}), frozenset({merge_key("B", "Two")}))


class _Jobs:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, str | None]] = []
        self.cancel = False

    def add_event(self, job_id: str, level: str, message: str, **kw: Any) -> int:
        self.events.append((level, message, kw.get("code")))
        return len(self.events)

    def set_progress(self, job_id: str, done: int, total: int | None) -> None: ...

    def finish(self, job_id: str, status: JobStatus, **kw: Any) -> None: ...

    def cancel_requested(self, job_id: str) -> bool:
        return self.cancel


class _Api:
    def __init__(
        self, songs: dict[str, list[dict[str, Any]]], fail: list[Exception] | None = None
    ) -> None:
        self.songs = songs
        self.fail = fail or []
        self.searches: list[str] = []
        self.saved: list[tuple[str, list[str], str | None]] = []

    def search_songs(self, query: str, count: int = 20) -> list[dict[str, Any]]:
        self.searches.append(query)
        return self.songs.get(query, [])

    def save_playlist(
        self, name: str, song_ids: Iterable[str], playlist_id: str | None = None
    ) -> str:
        if self.fail:
            raise self.fail.pop(0)
        self.saved.append((name, list(song_ids), playlist_id))
        return playlist_id or f"nd-{name}"


class _Store:
    def __init__(
        self, users: dict[str, tuple[str, str] | None], links: dict[str, list[dict[str, Any]]]
    ) -> None:
        self.users = users
        self._links = links
        self.pushes: list[tuple[str, str, dict[str, Any]]] = []
        self.cache: dict[str, str] = {}

    def account_users(self) -> list[str]:
        return list(self.users)

    def credentials(self, user_id: str) -> tuple[str, str] | None:
        return self.users.get(user_id)

    def links(self, user_id: str, *, enabled_only: bool = False) -> list[dict[str, Any]]:
        return [
            link
            for link in self._links.get(user_id, [])
            if link.get("enabled", True) or not enabled_only
        ]

    def record_push(self, user_id: str, spotify_id: str, **kw: Any) -> None:
        self.pushes.append((user_id, spotify_id, kw))

    def song_ids(self, keys: Iterable[str], *, fresh_after: str) -> dict[str, str]:
        return {k: v for k, v in self.cache.items() if k in set(keys)}

    def put_song(self, key: str, song_id: str) -> None:
        self.cache[key] = song_id


def _ctx(jobs: _Jobs) -> JobContext:
    job = Job("j1", JobKind.NAVIDROME_PUSH, "sync", JobStatus.RUNNING, "schedule")
    return JobContext(job, jobs, clock=lambda: 0.0)


SONGS = {
    "A One": [{"id": "sa", "artist": "A", "title": "One", "isrc": ["I1"]}],
    "B Two": [{"id": "sb", "artist": "B", "title": "Two"}],
}


def _run(store: _Store, api: _Api, jobs: _Jobs | None = None, **kw: Any) -> dict[str, Any]:
    tracks = {"p1": [A, B, C, A], "liked": [B]}
    return run_navidrome_push(
        _ctx(jobs or _Jobs()),
        store,
        client_for=lambda u, p: api,
        tracks_for=lambda uid, sid: tracks[sid],
        index=INDEX,
        now=NOW,
        **kw,
    )


def test_creates_then_replaces_with_library_tracks_in_order() -> None:
    store = _Store({"u1": ("sai", "pw")}, {"u1": [{"spotify_id": "p1", "name": "Mine"}]})
    api = _Api(SONGS)
    jobs = _Jobs()
    summary = _run(store, api, jobs)
    assert api.saved == [("Mine", ["sa", "sb"], None)]  # C isn't downloaded; A only once
    assert api.searches == ["A One", "B Two"]
    _, _, push = store.pushes[0]
    assert push == {"navidrome_id": "nd-Mine", "matched": 2, "in_library": 3, "total": 4}
    assert summary == {
        "users": 1,
        "playlists": 1,
        "matched": 2,
        "tracks": 4,
        "searched": 2,
        "failed": 0,
    }
    assert any("2 of 4 songs in Navidrome" in m for _, m, _ in jobs.events)
    # next run: song ids come from the cache and the playlist is replaced in place
    store._links["u1"][0]["navidrome_id"] = "nd-Mine"
    api.searches.clear()
    _run(store, api)
    assert api.searches == [] and api.saved[-1] == ("Mine", ["sa", "sb"], "nd-Mine")


def test_unmatched_tracks_are_reported_and_retried() -> None:
    store = _Store({"u1": ("sai", "pw")}, {"u1": [{"spotify_id": "p1", "name": "Mine"}]})
    api = _Api({"A One": SONGS["A One"]})
    jobs = _Jobs()
    _run(store, api, jobs)
    assert api.saved[0][1] == ["sa"]
    assert any("(2 not found there yet)" in m for _, m, _ in jobs.events)
    assert "tok:b two" not in store.cache


def test_recreates_a_playlist_deleted_in_navidrome() -> None:
    link = {"spotify_id": "p1", "name": "Mine", "navidrome_id": "gone"}
    store = _Store({"u1": ("sai", "pw")}, {"u1": [link]})
    api = _Api(SONGS, fail=[NavidromeError("not found", 70)])
    _run(store, api)
    assert api.saved == [("Mine", ["sa", "sb"], None)]
    assert store.pushes[0][2]["navidrome_id"] == "nd-Mine"


def test_bad_login_stops_that_user_other_errors_skip_one_playlist() -> None:
    links = [{"spotify_id": "p1", "name": "Mine"}, {"spotify_id": "liked", "name": "Liked Songs"}]
    store = _Store({"u1": ("sai", "old"), "u2": ("friend", "pw")}, {"u1": links, "u2": links[1:]})
    api = _Api(SONGS, fail=[NavidromeError("Wrong username or password", 40)])
    jobs = _Jobs()
    summary = _run(store, api, jobs)
    assert [(u, s, kw.get("error")) for u, s, kw in store.pushes] == [
        ("u1", "p1", "Wrong username or password"),
        ("u2", "liked", None),
    ]
    assert summary["failed"] == 1 and summary["playlists"] == 1
    assert (
        "warn",
        "Navidrome rejected this user's saved login; reconnect it on the Sync page",
        "navidrome_auth",
    ) in jobs.events

    api = _Api(SONGS, fail=[NavidromeError("server busy", 0), RuntimeError("spotify down")])
    store = _Store({"u1": ("sai", "pw")}, {"u1": [*links, {"spotify_id": "p1", "name": "Again"}]})
    summary = _run(store, api)
    assert summary["failed"] == 2 and summary["playlists"] == 1
    assert [kw.get("error") for _, _, kw in store.pushes] == ["server busy", "spotify down", None]


def test_scoping_budget_and_cancel() -> None:
    links = {
        "u1": [
            {"spotify_id": "p1", "name": "Mine"},
            {"spotify_id": "liked", "name": "Liked", "enabled": False},
        ],
        "u2": [{"spotify_id": "p1", "name": "X"}],
    }
    store = _Store({"u1": ("sai", "pw"), "u2": None, "u3": ("x", "y")}, links)
    api = _Api(SONGS)
    jobs = _Jobs()
    summary = _run(store, api, jobs, user_id="u1", playlist_id="p1", max_searches=1)
    assert summary["users"] == 1 and api.searches == ["A One"]  # budget of one search
    assert api.saved == [("Mine", ["sa"], None)]
    assert any(code == "budget" for _, _, code in jobs.events)
    assert _run(store, _Api(SONGS))["users"] == 1  # u2 has no login, u3 nothing mirrored
    jobs = _Jobs()
    jobs.cancel = True
    with pytest.raises(JobCancelled):
        _run(store, _Api(SONGS), jobs)
