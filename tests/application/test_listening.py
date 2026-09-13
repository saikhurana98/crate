"""poll_user / run_listen_poll against in-memory fakes (no DB, no network)."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

import pytest

from crate.application.auth import User
from crate.application.jobs import Job, JobCancelled, JobContext
from crate.application.listening import (
    RECENT_PAGE,
    play_from_recent,
    poll_user,
    run_listen_poll,
)
from crate.domain.jobs import JobKind, JobStatus
from crate.domain.listening import Play


def _item(i: int, at: str = "2026-09-01T10:00:00.500Z") -> dict[str, Any]:
    return {
        "id": f"t{i}",
        "uri": f"spotify:track:t{i}",
        "title": f"Song {i}",
        "artist": "Artist, Guest",
        "album": "LP",
        "played_at": at,
    }


def _page(n: int, start: int = 0) -> list[dict[str, Any]]:
    return [_item(start + i, f"2026-09-01T10:{(start + i) % 60:02d}:00Z") for i in range(n)]


class FakeReader:
    def __init__(
        self, pages: list[tuple[list[dict[str, Any]], int | None]], connected: bool = True
    ):
        self.pages = list(pages)
        self.calls: list[int | None] = []
        self._connected = connected

    def connected(self, user_id: str) -> bool:
        return self._connected

    def recent_plays_after(
        self, user_id: str, after_ms: int | None
    ) -> tuple[list[dict[str, Any]], int | None]:
        self.calls.append(after_ms)
        return self.pages.pop(0) if self.pages else ([], after_ms)


class FakeLedger:
    def __init__(self, cursor: int | None = None) -> None:
        self.cursor = cursor
        self.plays: set[tuple[str, str, str]] = set()
        self.tracks: dict[str, dict[str, Any]] = {}

    def recent_cursor(self, user_id: str) -> int | None:
        return self.cursor

    def set_recent_cursor(self, user_id: str, after_ms: int | None) -> None:
        self.cursor = after_ms

    def add_plays(self, user_id: str, plays: Iterable[Play], source: str) -> int:
        new = 0
        for p in plays:
            key = (user_id, p.played_at, p.merge_key)
            if key not in self.plays:
                self.plays.add(key)
                new += 1
        return new

    def upsert_tracks(self, tracks: Iterable[dict[str, Any]]) -> None:
        for t in tracks:
            self.tracks[t["id"]] = t


def test_play_from_recent_maps_and_normalises() -> None:
    play = play_from_recent(_item(1))
    assert play is not None
    assert play.played_at == "2026-09-01T10:00:00Z" and play.track_uri == "spotify:track:t1"
    assert play.merge_key == "tok:artist song 1"  # primary artist only
    assert play.album == "LP" and play.ms_played is None


@pytest.mark.parametrize(
    "bad",
    [{**_item(1), "title": None}, {**_item(1), "played_at": None}, {**_item(1), "played_at": "x"}],
)
def test_play_from_recent_skips_unusable_items(bad: dict[str, Any]) -> None:
    assert play_from_recent(bad) is None


def test_first_poll_stores_plays_and_sets_cursor() -> None:
    reader = FakeReader([(_page(3), 300)])
    ledger = FakeLedger()
    res = poll_user("u1", reader, ledger)
    assert (res.fetched, res.inserted, res.pages, res.gap_suspected) == (3, 3, 1, False)
    assert ledger.cursor == 300 and reader.calls == [None]
    assert set(ledger.tracks) == {"t0", "t1", "t2"}  # metadata cached for tiers/previews


def test_next_poll_follows_the_cursor_and_is_idempotent() -> None:
    ledger = FakeLedger()
    poll_user("u1", FakeReader([(_page(3), 300)]), ledger)
    reader = FakeReader([(_page(3), 300)])  # the same plays come back
    res = poll_user("u1", reader, ledger)
    assert reader.calls == [300] and res.inserted == 0


def test_full_pages_are_followed_until_a_partial_one() -> None:
    reader = FakeReader([(_page(RECENT_PAGE), 500), (_page(10, RECENT_PAGE), 600)])
    res = poll_user("u1", reader, FakeLedger(cursor=100))
    assert res.pages == 2 and res.fetched == RECENT_PAGE + 10
    assert reader.calls == [100, 500] and res.gap_suspected is False


def test_still_full_after_max_pages_flags_a_possible_gap() -> None:
    pages = [(_page(RECENT_PAGE, i * RECENT_PAGE), 1000 + i) for i in range(4)]
    res = poll_user("u1", FakeReader(pages), FakeLedger(cursor=1), max_pages=2)
    assert res.pages == 2 and res.gap_suspected is True


def test_full_first_poll_is_not_a_gap() -> None:
    res = poll_user("u1", FakeReader([(_page(RECENT_PAGE), 500)]), FakeLedger(), max_pages=1)
    assert res.gap_suspected is False  # no cursor yet: that's just the last 50 plays


def test_paging_stops_when_the_cursor_does_not_move() -> None:
    reader = FakeReader([(_page(RECENT_PAGE), 100), (_page(RECENT_PAGE), 100)])
    res = poll_user("u1", reader, FakeLedger(cursor=100))
    assert res.pages == 1


class FakeJobStore:
    def __init__(self) -> None:
        self.events: list[tuple[str, str | None, str]] = []
        self.cancel = False

    def add_event(self, job_id: str, level: str, message: str, **kw: Any) -> int:
        self.events.append((level, kw.get("code"), message))
        return len(self.events)

    def set_progress(self, job_id: str, done: int, total: int | None) -> None: ...

    def finish(self, job_id: str, status: JobStatus, **kw: Any) -> None: ...

    def cancel_requested(self, job_id: str) -> bool:
        return self.cancel


def _ctx(store: FakeJobStore) -> JobContext:
    job = Job(
        id="j",
        kind=JobKind.LISTEN_POLL,
        lane="sync",
        status=JobStatus.RUNNING,
        triggered_by="schedule",
    )
    return JobContext(job, store)


class RoutedReader(FakeReader):
    """Per-user behaviour: ok / not connected / raises."""

    def __init__(self) -> None:
        super().__init__([])

    def connected(self, user_id: str) -> bool:
        return user_id != "offline"

    def recent_plays_after(
        self, user_id: str, after_ms: int | None
    ) -> tuple[list[dict[str, Any]], int | None]:
        if user_id == "broken":
            raise RuntimeError("token revoked")
        return _page(2), 200


def test_run_listen_poll_counts_skips_and_isolates_failures() -> None:
    store = FakeJobStore()
    users = [
        User(id="ok", spotify_id="s1", display_name="Sai"),
        User(id="offline", spotify_id="s2", display_name="Friend"),
        User(id="broken", spotify_id="s3", display_name="Other"),
    ]
    summary = run_listen_poll(_ctx(store), users, RoutedReader(), FakeLedger())
    assert summary == {"users": 1, "new_plays": 2, "skipped": 1, "failed": 1}
    codes = [e[1] for e in store.events]
    assert "poll_failed" in codes and "plays" in codes and codes[-1] == "summary"
    assert all("Song" not in e[2] for e in store.events)  # no track names in global lines


def test_run_listen_poll_warns_on_gap() -> None:
    store = FakeJobStore()
    ledger = FakeLedger(cursor=1)
    reader = FakeReader([(_page(RECENT_PAGE, i * RECENT_PAGE), 10 + i) for i in range(5)])
    run_listen_poll(_ctx(store), [User(id="u", spotify_id="s", display_name="Sai")], reader, ledger)
    assert any(code == "gap" for _, code, _ in store.events)


def test_run_listen_poll_honours_cancellation() -> None:
    store = FakeJobStore()
    store.cancel = True
    with pytest.raises(JobCancelled):
        run_listen_poll(
            _ctx(store),
            [User(id="u", spotify_id="s", display_name="S")],
            FakeReader([]),
            FakeLedger(),
        )
