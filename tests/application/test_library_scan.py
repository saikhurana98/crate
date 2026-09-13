"""run_dedup_scan end to end: real SQLite index + job log, fake walker/analyser/AcoustID."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from crate.application.identify import parse_acoustid_match
from crate.application.jobs import JobCancelled, JobContext
from crate.application.library_scan import FileAnalysis, RateLimiter, run_dedup_scan
from crate.domain.chromaprint import unpack_raw
from crate.domain.jobs import JobKind
from crate.domain.library import TrackTags
from crate.domain.library_scan import DiskEntry
from crate.infrastructure.sqlite.db import connect
from crate.infrastructure.sqlite.job_store import SqliteJobStore
from crate.infrastructure.sqlite.library_store import SqliteLibraryStore

GOLDEN = json.loads(
    (Path(__file__).parents[1] / "domain" / "fixtures" / "chromaprint_golden.json").read_text()
)


def analysis(title: str = "Song", lossless: bool = True) -> FileAnalysis:
    fp = GOLDEN["sweep20"]
    return FileAnalysis(
        codec="flac" if lossless else "mp3",
        container="flac" if lossless else "mp3",
        lossless=lossless,
        bitrate=None if lossless else 320_000,
        bit_depth=16 if lossless else None,
        sample_rate=44100,
        channels=2,
        duration_s=200.0,
        tags=TrackTags(artist="Artist", title=title, album="LP"),
        has_cover=True,
        fp_duration=fp["duration"],
        fp_compressed=fp["compressed"],
        fp_raw=fp["raw"][:140],
    )


class World:
    def __init__(self, tmp_path: Path) -> None:
        self.root = tmp_path / "library"
        self.root.mkdir()
        conn = connect(tmp_path / "crate.db")
        self.store = SqliteLibraryStore(conn)
        self.jobs = SqliteJobStore(conn)
        self.jobs.enqueue(JobKind.DEDUP_SCAN, triggered_by="manual")
        job = self.jobs.claim_next("scan")
        assert job is not None
        self.job = job
        self.conn = conn
        self.disk: list[DiskEntry] = []
        self.fail: set[str] = set()
        self.analysed: list[str] = []
        self.lookups: list[str] = []
        self.lookup_reply: Any = {"status": "ok", "results": [{"id": "aid-1", "score": 0.97}]}

    def walk(self, root: Path) -> list[DiskEntry]:
        return list(self.disk)

    def analyze(self, path: Path) -> FileAnalysis:
        rel = path.relative_to(self.root).as_posix()
        self.analysed.append(rel)
        if rel in self.fail:
            raise RuntimeError("ffprobe: invalid data")
        return analysis(title=rel)

    def lookup(self, duration: int, fp: str) -> dict[str, Any]:
        self.lookups.append(fp[:8])
        if isinstance(self.lookup_reply, Exception):
            raise self.lookup_reply
        return dict(self.lookup_reply)

    def run(self, *, with_lookup: bool = True) -> dict[str, Any]:
        return run_dedup_scan(
            JobContext(self.job, self.jobs),
            self.root,
            walk=self.walk,
            store=self.store,
            analyze=self.analyze,
            lookup=self.lookup if with_lookup else None,
            limiter=RateLimiter(0),  # no spacing in tests
        )

    def codes(self) -> list[str]:
        return [
            r[0]
            for r in self.conn.execute("SELECT code FROM job_events WHERE job_id=?", (self.job.id,))
        ]


def test_first_scan_analyses_and_identifies_everything(tmp_path: Path) -> None:
    w = World(tmp_path)
    w.disk = [DiskEntry("a/1.flac", 100, 1, 11), DiskEntry("a/2.mp3", 50, 1, 12)]
    s = w.run()
    assert (s["new"], s["analysed"], s["looked_up"], s["identified"]) == (2, 2, 2, 2)
    assert (
        s["index"]["files"] == 2 and s["index"]["identified"] == 2 and s["index"]["lossless"] == 2
    )
    fid = w.store.indexed()[0].id
    row = w.store.get(fid)
    assert row is not None and row["acoustid_id"] == "aid-1" and row["tag_key"]
    assert unpack_raw(row["fp_raw"]) == GOLDEN["sweep20"]["raw"][:140]
    assert {"walk", "summary"} <= set(w.codes())


def test_rescan_is_incremental(tmp_path: Path) -> None:
    w = World(tmp_path)
    w.disk = [DiskEntry("a/1.flac", 100, 1, 11), DiskEntry("a/2.flac", 50, 1, 12)]
    w.run()
    w.analysed.clear()
    w.lookups.clear()
    w.disk = [
        DiskEntry("a/1.flac", 100, 1, 11),
        DiskEntry("b/2.flac", 50, 1, 12),
        DiskEntry("a/3.flac", 7, 1, 13),
    ]
    s = w.run()
    assert (s["new"], s["moved"], s["changed"], s["missing"]) == (1, 1, 0, 0)
    assert w.analysed == ["a/3.flac"] and len(w.lookups) == 1  # only the new file


def test_a_changed_file_is_re_analysed_and_a_gone_one_marked_missing(tmp_path: Path) -> None:
    w = World(tmp_path)
    w.disk = [DiskEntry("a/1.flac", 100, 1, 11), DiskEntry("a/2.flac", 50, 1, 12)]
    w.run()
    w.analysed.clear()
    w.disk = [DiskEntry("a/1.flac", 101, 2, 11)]
    s = w.run()
    assert (s["changed"], s["missing"]) == (1, 1) and w.analysed == ["a/1.flac"]
    assert s["index"]["files"] == 1 and s["index"]["missing"] == 1


def test_an_empty_walk_never_marks_the_library_missing(tmp_path: Path) -> None:
    w = World(tmp_path)
    w.disk = [DiskEntry("a/1.flac", 100, 1, 11)]
    w.run()
    w.disk = []
    with pytest.raises(RuntimeError, match="mounted"):
        w.run()
    assert w.store.stats()["missing"] == 0


def test_a_missing_root_fails_fast(tmp_path: Path) -> None:
    w = World(tmp_path)
    w.root.rmdir()
    with pytest.raises(RuntimeError, match="not a directory"):
        w.run()


def test_one_bad_file_is_recorded_and_skipped(tmp_path: Path) -> None:
    w = World(tmp_path)
    w.disk = [DiskEntry("a/bad.flac", 1, 1, 1), DiskEntry("a/good.flac", 2, 1, 2)]
    w.fail = {"a/bad.flac"}
    s = w.run()
    assert (s["analysed"], s["analyse_failed"]) == (2, 1)
    assert s["index"]["errors"] == 1 and "analyze_failed" in w.codes()
    w.analysed.clear()
    w.run()
    assert w.analysed == []  # not retried until the file changes


def test_lookup_gives_up_after_a_streak_and_leaves_the_rest_pending(tmp_path: Path) -> None:
    w = World(tmp_path)
    w.disk = [DiskEntry(f"a/{i}.flac", i + 1, 1, i + 1) for i in range(8)]
    w.lookup_reply = RuntimeError("503")
    s = w.run()
    assert s["looked_up"] == 0 and len(w.lookups) == 5 and "lookup_stopped" in w.codes()
    w.lookup_reply = {"status": "ok", "results": []}
    assert w.run()["looked_up"] == 8  # retried next time; no match is still "looked up"


def test_lookups_off_without_a_key(tmp_path: Path) -> None:
    w = World(tmp_path)
    w.disk = [DiskEntry("a/1.flac", 1, 1, 1)]
    s = w.run(with_lookup=False)
    assert s["looked_up"] == 0 and "lookup_off" in w.codes()


def test_cancel_stops_between_files(tmp_path: Path) -> None:
    w = World(tmp_path)
    w.disk = [DiskEntry("a/1.flac", 1, 1, 1)]
    w.jobs.request_cancel(w.job.id)
    with pytest.raises(JobCancelled):
        w.run()


def test_rate_limiter_spaces_calls() -> None:
    clock = [0.0]
    slept: list[float] = []

    def sleep(s: float) -> None:
        slept.append(round(s, 3))
        clock[0] += s

    limiter = RateLimiter(2.5, clock=lambda: clock[0], sleep=sleep)
    for _ in range(3):
        limiter.wait()
    assert slept == [0.4, 0.4]


def test_parse_acoustid_match() -> None:
    data = {
        "status": "ok",
        "results": [
            {
                "id": "aid-9",
                "score": 0.91,
                "recordings": [{"id": "mb-1", "title": "T", "artists": [{"name": "A"}]}],
            }
        ],
    }
    m = parse_acoustid_match(data)
    assert m is not None and (m.track_id, m.score, m.mbid, m.artist, m.title) == (
        "aid-9",
        0.91,
        "mb-1",
        "A",
        "T",
    )
    bare = parse_acoustid_match({"status": "ok", "results": [{"id": "aid-2", "score": 0.5}]})
    assert bare is not None and bare.mbid is None  # a track id alone still groups duplicates
    assert parse_acoustid_match({"status": "ok", "results": []}) is None
    with pytest.raises(RuntimeError, match="invalid API key"):
        parse_acoustid_match(
            {"status": "error", "error": {"code": 4, "message": "invalid API key"}}
        )
