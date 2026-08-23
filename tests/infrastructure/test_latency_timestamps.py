"""Phase C: dual-latency timestamps (first_playable_at / final_flac_at) and the
shared feed. Uses a real temp SQLite DB; now_iso is monkeypatched to a controlled
clock so the two latencies get distinct, assertable values."""

from __future__ import annotations

from pathlib import Path

import pytest

from crate.application.download import DownloadReport
from crate.domain.audio import AudioInfo, QualityVerdict
from crate.domain.download import DownloadOutcome
from crate.infrastructure.sqlite import store as store_mod
from crate.infrastructure.sqlite.db import connect
from crate.infrastructure.sqlite.store import RequestStore


class _Clock:
    """Monotonic ISO clock the store reads via the patched now_iso; each call
    advances one second so created_at < first_playable_at < final_flac_at."""

    def __init__(self) -> None:
        self.t = 0

    def iso(self) -> str:
        self.t += 1
        # zero-padded so ISO string order == chronological order
        return f"2026-01-01T00:00:{self.t:02d}+00:00"


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    c = _Clock()
    monkeypatch.setattr(store_mod, "now_iso", c.iso)
    return c


def _store(tmp_path: Path) -> RequestStore:
    return RequestStore(connect(tmp_path / "crate.db"))


def _report(*, lossless: bool, source: str = "qobuz", tier: int = 1) -> DownloadReport:
    outcome = DownloadOutcome(Path(f"/nas/{source}.flac"), tier, source, 4.2, "q")
    info = AudioInfo("flac", "flac", 96000, 2, 24, 1_800_000, 248.0, 94_000_000)
    return DownloadReport("q", outcome, info, QualityVerdict("lbl", lossless), None)


def _row(store: RequestStore, rid: str) -> dict[str, object]:
    return next(r for r in store.list_feed() if r["id"] == rid)


def test_lossless_finish_sets_both_latencies(tmp_path: Path, clock: _Clock) -> None:
    store = _store(tmp_path)
    rid = store.enqueue("q")  # created_at = t1
    store.finish_request(rid, _report(lossless=True))  # playable + final = t2
    row = _row(store, rid)
    assert row["first_playable_at"] is not None
    assert row["final_flac_at"] is not None
    assert row["ttf_playable_s"] == 1.0
    assert row["ttf_final_s"] == 1.0


def test_lossy_finish_sets_playable_only(tmp_path: Path, clock: _Clock) -> None:
    store = _store(tmp_path)
    rid = store.enqueue("q")
    store.finish_request(rid, _report(lossless=False, source="ytdlp", tier=3))
    row = _row(store, rid)
    assert row["first_playable_at"] is not None
    assert row["final_flac_at"] is None  # no FLAC yet
    assert row["ttf_playable_s"] == 1.0
    assert row["ttf_final_s"] is None


def test_upgrade_splits_the_two_latencies(tmp_path: Path, clock: _Clock) -> None:
    """The lossy rip lands first (playable), then the FLAC upgrade re-enters
    finish_request: first_playable_at is COALESCE-once (unchanged) while
    final_flac_at is stamped at the upgrade — two distinct latencies."""
    store = _store(tmp_path)
    rid = store.enqueue("q")  # t1
    store.finish_request(rid, _report(lossless=False, source="ytdlp", tier=3))  # playable @ t2
    playable_at = _row(store, rid)["first_playable_at"]
    store.finish_request(rid, _report(lossless=True))  # FLAC upgrade @ t3
    row = _row(store, rid)
    assert row["first_playable_at"] == playable_at  # never overwritten
    assert row["final_flac_at"] is not None
    assert row["final_flac_at"] != row["first_playable_at"]
    assert row["ttf_playable_s"] == 1.0
    assert row["ttf_final_s"] == 2.0  # request -> FLAC spans both steps


def test_complete_as_duplicate_sets_both(tmp_path: Path, clock: _Clock) -> None:
    store = _store(tmp_path)
    rid = store.enqueue("q")
    existing = {
        "tier_won": 1,
        "source_won": "qobuz",
        "path": "/nas/lib.flac",
        "size_bytes": 1,
        "codec": "flac",
        "bitrate": 1,
        "bit_depth": 24,
        "sample_rate": 96000,
        "duration_s": 200.0,
        "lossless": 1,
        "quality_label": "lbl",
    }
    store.complete_as_duplicate(rid, existing)
    row = _row(store, rid)
    assert row["ttf_playable_s"] is not None
    assert row["ttf_final_s"] is not None  # a de-dup reuse is instantly final FLAC


def test_lossy_duplicate_leaves_final_unset(tmp_path: Path, clock: _Clock) -> None:
    store = _store(tmp_path)
    rid = store.enqueue("q")
    existing = {
        "tier_won": 3,
        "source_won": "ytdlp",
        "path": "/nas/lib.mp3",
        "size_bytes": 1,
        "codec": "mp3",
        "bitrate": 320,
        "bit_depth": None,
        "sample_rate": 44100,
        "duration_s": 200.0,
        "lossless": 0,
        "quality_label": "mp3",
    }
    store.complete_as_duplicate(rid, existing)
    row = _row(store, rid)
    assert row["first_playable_at"] is not None
    assert row["final_flac_at"] is None


def test_failed_request_has_no_latencies(tmp_path: Path, clock: _Clock) -> None:
    store = _store(tmp_path)
    rid = store.enqueue("q")
    store.fail_request(rid, "boom")
    row = _row(store, rid)
    assert row["ttf_playable_s"] is None
    assert row["ttf_final_s"] is None
    assert row["status"] == "failed"


def test_feed_carries_requester_and_shape(tmp_path: Path, clock: _Clock) -> None:
    store = _store(tmp_path)
    rid = store.enqueue("q")
    store.finish_request(rid, _report(lossless=True))
    row = _row(store, rid)
    # everyone-sees-everything feed row shape
    for key in ("user_id", "user_name", "user_avatar", "ttf_playable_s", "ttf_final_s"):
        assert key in row
    assert row["user_id"] == "service"  # default attribution
    assert row["query"] == "q"


def test_backfill_maps_legacy_rows(tmp_path: Path) -> None:
    """The 010 backfill: a pre-Phase-C `done` row becomes playable at its finish
    time, and — only if its download is lossless — final at that same time. Runs
    the migration's own UPDATE statements against hand-seeded legacy rows so the
    shipped SQL is what's verified."""
    conn = connect(tmp_path / "crate.db")  # all migrations applied; columns exist
    # Two legacy `done` rows with the new columns still NULL (as they'd be pre-010):
    # one with a lossless download, one lossy.
    for rid, path, lossless in (("flacrow", "/nas/a.flac", 1), ("lossyrow", "/nas/b.mp3", 0)):
        conn.execute(
            "INSERT INTO requests(id, query, requested_via, status, created_at, finished_at, "
            "first_playable_at, final_flac_at) "
            "VALUES (?, 'q', 'explo', 'done', '2026-01-01T00:00:00+00:00', "
            "'2026-01-01T00:05:00+00:00', NULL, NULL)",
            (rid,),
        )
        conn.execute(
            "INSERT INTO downloads(request_id, path, size_bytes, codec, lossless, quality_label) "
            "VALUES (?, ?, 1, 'x', ?, 'lbl')",
            (rid, path, lossless),
        )
    # Re-run the two UPDATE statements straight from the shipped migration file.
    sql = (Path(store_mod.__file__).parent / "migrations" / "010_latency.sql").read_text()
    for chunk in sql.split(";"):
        # drop the comment lines each chunk carries, keep only real statement text
        body = "\n".join(ln for ln in chunk.splitlines() if not ln.strip().startswith("--"))
        if body.strip().upper().startswith("UPDATE"):
            conn.execute(body)

    store = RequestStore(conn)
    flac = _row(store, "flacrow")
    lossy = _row(store, "lossyrow")
    assert flac["first_playable_at"] == "2026-01-01T00:05:00+00:00"
    assert flac["final_flac_at"] == "2026-01-01T00:05:00+00:00"  # lossless -> final too
    assert flac["ttf_playable_s"] == 300.0
    assert lossy["first_playable_at"] == "2026-01-01T00:05:00+00:00"
    assert lossy["final_flac_at"] is None  # lossy legacy row never got a FLAC


def test_feed_newest_first(tmp_path: Path, clock: _Clock) -> None:
    store = _store(tmp_path)
    a = store.enqueue("older")
    b = store.enqueue("newer")
    store.finish_request(a, _report(lossless=True))
    store.finish_request(b, _report(lossless=True))
    ids = [r["id"] for r in store.list_feed()]
    assert ids.index(b) < ids.index(a)  # b finished last -> sorts first
