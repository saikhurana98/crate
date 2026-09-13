"""Streaming-history parsing + the import job body, against fakes."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

import pytest

from crate.application.history_import import (
    MIN_PLAY_MS,
    detect_format,
    parse_entry,
    run_history_import,
)
from crate.application.jobs import Job, JobCancelled, JobContext
from crate.domain.jobs import JobKind, JobStatus
from crate.domain.listening import Play


def ext(**kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "ts": "2021-03-04T05:06:07Z",
        "ms_played": 200_000,
        "master_metadata_track_name": "Song",
        "master_metadata_album_artist_name": "Artist",
        "master_metadata_album_album_name": "LP",
        "spotify_track_uri": "spotify:track:abc",
        "spotify_episode_uri": None,
        "episode_name": None,
        "skipped": False,
    }
    base.update(kw)
    return base


def short(**kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "endTime": "2023-01-01 12:34",
        "artistName": "Artist",
        "trackName": "Song",
        "msPlayed": 200_000,
    }
    base.update(kw)
    return base


def test_detect_format() -> None:
    assert detect_format(ext()) == "extended"
    assert detect_format(short()) == "short"
    assert detect_format({"foo": 1}) is None


def test_extended_listen_becomes_a_play() -> None:
    play, reason = parse_entry(ext(), "extended")
    assert reason is None and play is not None
    assert play.played_at == "2021-03-04T05:06:07Z" and play.track_uri == "spotify:track:abc"
    assert play.merge_key == "tok:artist song" and play.ms_played == 200_000
    assert play.album == "LP"


@pytest.mark.parametrize(
    ("entry", "reason"),
    [
        (ext(spotify_episode_uri="spotify:episode:x", master_metadata_track_name=None), "podcast"),
        (ext(episode_name="Ep 1"), "podcast"),
        (ext(audiobook_title="Book"), "podcast"),
        (ext(master_metadata_track_name=None), "no_track"),
        (ext(ms_played=MIN_PLAY_MS - 1), "too_short"),
        (ext(skipped=True, ms_played=45_000), "skipped"),
        (ext(ts="not a time"), "bad_time"),
        (ext(ms_played="garbage"), "too_short"),
    ],
)
def test_extended_filters(entry: dict[str, Any], reason: str) -> None:
    assert parse_entry(entry, "extended") == (None, reason)


def test_long_skip_still_counts() -> None:
    play, _ = parse_entry(ext(skipped=True, ms_played=90_000), "extended")
    assert play is not None  # skipped after 90 s of listening = a real listen


def test_local_file_is_kept_without_uri() -> None:
    play, _ = parse_entry(ext(spotify_track_uri="spotify:local:Artist:LP:Song:200"), "extended")
    assert play is not None and play.track_uri is None


def test_short_format_play_and_filter() -> None:
    play, _ = parse_entry(short(), "short")
    assert play is not None and play.played_at == "2023-01-01T12:34:00Z"
    assert play.track_uri is None
    assert parse_entry(short(msPlayed=1000), "short") == (None, "too_short")


class FakeFile:
    def __init__(self, name: str, entries: list[dict[str, Any]]) -> None:
        self._name = name
        self.entries = entries
        self.loads = 0

    @property
    def name(self) -> str:
        return self._name

    def load(self) -> list[dict[str, Any]]:
        self.loads += 1
        return self.entries


class FakeHistoryStore:
    def __init__(self) -> None:
        self.plays: list[tuple[str, Play, str]] = []
        self.batches: list[int] = []
        self.deleted: tuple[str, str, str] | None = None
        self.range: tuple[str, str, str] | None = None

    def add_plays(self, user_id: str, plays: Iterable[Play], source: str) -> int:
        rows = list(plays)
        self.batches.append(len(rows))
        self.plays.extend((user_id, p, source) for p in rows)
        return len(rows)

    def delete_recent_in_range(self, user_id: str, start: str, end: str) -> int:
        self.deleted = (user_id, start, end)
        return 2

    def set_export_range(self, user_id: str, start: str, end: str) -> None:
        self.range = (user_id, start, end)


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


def _ctx(jobs: FakeJobStore) -> JobContext:
    job = Job(
        id="j",
        kind=JobKind.HISTORY_IMPORT,
        lane="sync",
        status=JobStatus.RUNNING,
        triggered_by="manual",
        user_id="u1",
    )
    return JobContext(job, jobs, clock=lambda: 0.0)


def test_import_writes_plays_records_span_and_folds_overlap() -> None:
    files = [
        FakeFile(
            "Streaming_History_Audio_2019-2020_0.json",
            [ext(ts="2019-01-01T00:00:00Z"), ext(ms_played=10)],
        ),
        FakeFile("Streaming_History_Audio_2021_1.json", [ext(ts="2021-06-01T00:00:00Z"), {"x": 1}]),
    ]
    store, jobs = FakeHistoryStore(), FakeJobStore()
    summary = run_history_import(_ctx(jobs), "u1", files, store)
    assert summary["files"] == 2 and summary["rows"] == 4 and summary["imported"] == 2
    assert summary["skipped"] == {"too_short": 1, "unknown_format": 1}
    assert summary["range"] == ["2019-01-01T00:00:00Z", "2021-06-01T00:00:00Z"]
    assert {src for _, _, src in store.plays} == {"export"}
    assert store.deleted == ("u1", "2019-01-01T00:00:00Z", "2021-06-01T00:00:00Z")
    assert store.range == ("u1", "2019-01-01T00:00:00Z", "2021-06-01T00:00:00Z")
    assert all(f.loads == 1 for f in files)  # each file loaded exactly once
    assert jobs.events[-1][1] == "import_done"


def test_import_batches_large_files(monkeypatch: pytest.MonkeyPatch) -> None:
    import crate.application.history_import as mod

    monkeypatch.setattr(mod, "_BATCH", 2)
    entries = [ext(ts=f"2021-01-01T00:00:0{i}Z") for i in range(5)]
    store = FakeHistoryStore()
    run_history_import(_ctx(FakeJobStore()), "u1", [FakeFile("a.json", entries)], store)
    assert store.batches == [2, 2, 1]


def test_short_export_warns_but_imports() -> None:
    jobs = FakeJobStore()
    files = [FakeFile("StreamingHistory_music_0.json", [short()])]
    summary = run_history_import(_ctx(jobs), "u1", files, FakeHistoryStore())
    assert summary["imported"] == 1
    assert any(code == "short_export" for _, code, _ in jobs.events)


def test_empty_import_warns_and_records_nothing() -> None:
    jobs, store = FakeJobStore(), FakeHistoryStore()
    summary = run_history_import(_ctx(jobs), "u1", [FakeFile("a.json", [ext(ms_played=1)])], store)
    assert summary["imported"] == 0 and summary["range"] is None
    assert store.range is None and store.deleted is None
    assert jobs.events[-1][1] == "empty_import"


def test_import_honours_cancellation() -> None:
    jobs = FakeJobStore()
    jobs.cancel = True
    with pytest.raises(JobCancelled):
        run_history_import(_ctx(jobs), "u1", [FakeFile("a.json", [ext()])], FakeHistoryStore())
