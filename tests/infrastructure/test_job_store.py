"""SqliteJobStore on a real temp DB: single-flight, claim race, lanes, cancel,
event cursor + per-user visibility, restart reaping, retention."""

from __future__ import annotations

import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from crate.application.jobs import Job
from crate.domain.jobs import JobKind, JobStatus
from crate.infrastructure.sqlite.db import connect
from crate.infrastructure.sqlite.job_store import SqliteJobStore


def _store(tmp_path: Path) -> SqliteJobStore:
    return SqliteJobStore(connect(tmp_path / "crate.db"))


def test_enqueue_claim_finish_lifecycle(tmp_path: Path) -> None:
    s = _store(tmp_path)
    jid, existing = s.enqueue(JobKind.LIBRARY_SYNC, triggered_by="manual", user_id="service")
    assert existing is False
    assert s.events(jid)[0]["code"] == "queued"
    job = s.claim_next("sync")
    assert job is not None and job.id == jid
    assert job.status is JobStatus.RUNNING and job.user_id == "service"
    assert s.claim_next("sync") is None  # nothing left
    s.finish(jid, JobStatus.DONE, summary={"playlists": 4})
    got = s.get(jid)
    assert got is not None and got["status"] == "done" and got["summary"] == {"playlists": 4}
    assert got["started_at"] and got["finished_at"] and got["cancel_requested"] is False


def test_single_flight_returns_the_active_job_until_it_finishes(tmp_path: Path) -> None:
    s = _store(tmp_path)
    first, _ = s.enqueue(JobKind.LIBRARY_SYNC, triggered_by="schedule")
    again, existing = s.enqueue(JobKind.LIBRARY_SYNC, triggered_by="manual", user_id="service")
    assert (again, existing) == (first, True)
    s.claim_next("sync")  # running still counts as active
    assert s.enqueue(JobKind.LIBRARY_SYNC, triggered_by="manual") == (first, True)
    s.finish(first, JobStatus.DONE)
    fresh, existing = s.enqueue(JobKind.LIBRARY_SYNC, triggered_by="manual")
    assert fresh != first and existing is False


def test_non_single_flight_kinds_can_stack(tmp_path: Path) -> None:
    s = _store(tmp_path)
    a, _ = s.enqueue(JobKind.HISTORY_IMPORT, triggered_by="manual")
    b, existing = s.enqueue(JobKind.HISTORY_IMPORT, triggered_by="manual")
    assert a != b and existing is False


def test_params_round_trip(tmp_path: Path) -> None:
    s = _store(tmp_path)
    s.enqueue(JobKind.HISTORY_IMPORT, triggered_by="manual", params={"upload": "x.zip"})
    job = s.claim_next("sync")
    assert job is not None and job.params == {"upload": "x.zip"}


def test_lanes_are_isolated(tmp_path: Path) -> None:
    s = _store(tmp_path)
    s.enqueue(JobKind.DEDUP_SCAN, triggered_by="schedule")
    assert s.claim_next("sync") is None  # the sync lane never picks up a scan
    job = s.claim_next("scan")
    assert job is not None and job.kind is JobKind.DEDUP_SCAN


def test_claim_race_has_exactly_one_winner(tmp_path: Path) -> None:
    db = tmp_path / "crate.db"
    SqliteJobStore(connect(db)).enqueue(JobKind.LIBRARY_SYNC, triggered_by="schedule")
    stores = [SqliteJobStore(connect(db)) for _ in range(4)]  # separate connections
    barrier = threading.Barrier(len(stores))
    won: list[Job | None] = []

    def claim(s: SqliteJobStore) -> None:
        barrier.wait()
        won.append(s.claim_next("sync"))

    threads = [threading.Thread(target=claim, args=(s,)) for s in stores]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(1 for j in won if j is not None) == 1


def test_cancel_queued_running_and_finished(tmp_path: Path) -> None:
    s = _store(tmp_path)
    queued, _ = s.enqueue(JobKind.HISTORY_IMPORT, triggered_by="manual")
    assert s.request_cancel(queued) == "cancelled"
    assert s.claim_next("sync") is None  # a cancelled job is never claimed
    assert s.get(queued)["status"] == "cancelled"  # type: ignore[index]

    running, _ = s.enqueue(JobKind.HISTORY_IMPORT, triggered_by="manual")
    s.claim_next("sync")
    assert s.request_cancel(running) == "cancelling"
    assert s.cancel_requested(running) is True

    s.finish(running, JobStatus.CANCELLED)
    assert s.request_cancel(running) is None  # already finished
    assert s.request_cancel("nope") is None
    assert s.cancel_requested("nope") is False


def test_events_cursor_and_per_user_visibility(tmp_path: Path) -> None:
    s = _store(tmp_path)
    jid, _ = s.enqueue(JobKind.LIBRARY_SYNC, triggered_by="schedule")
    s.add_event(jid, "info", "global line")
    s.add_event(jid, "info", "mine", user_id="u1")
    s.add_event(jid, "info", "theirs", user_id="u2", data={"n": 1})
    mine = [e["message"] for e in s.events(jid, viewer="u1")]
    assert "global line" in mine and "mine" in mine and "theirs" not in mine
    cursor = s.events(jid, viewer="u1")[0]["id"]
    assert [e["message"] for e in s.events(jid, after=cursor, viewer="u1")] == [
        "global line",
        "mine",
    ]
    theirs = s.events_since(0, viewer="u2")
    assert [e["message"] for e in theirs][-1] == "theirs" and theirs[-1]["data"] == {"n": 1}
    assert all(e["message"] != "mine" for e in theirs)


def test_progress_and_heartbeat(tmp_path: Path) -> None:
    s = _store(tmp_path)
    jid, _ = s.enqueue(JobKind.DEDUP_SCAN, triggered_by="manual")
    s.set_progress(jid, 5, 10)
    s.heartbeat(jid)
    got = s.get(jid)
    assert got is not None and (got["progress_done"], got["progress_total"]) == (5, 10)
    assert got["heartbeat_at"]


def test_list_jobs_filters_by_kind_newest_first(tmp_path: Path) -> None:
    s = _store(tmp_path)
    s.enqueue(JobKind.HISTORY_IMPORT, triggered_by="manual")
    s.enqueue(JobKind.DEDUP_SCAN, triggered_by="manual")
    assert {j["kind"] for j in s.list_jobs()} == {"history_import", "dedup_scan"}
    assert [j["kind"] for j in s.list_jobs(kind="dedup_scan")] == ["dedup_scan"]
    assert len(s.list_jobs(limit=1)) == 1


def test_last_created_drives_the_scheduler(tmp_path: Path) -> None:
    s = _store(tmp_path)
    assert s.last_created(JobKind.LIBRARY_SYNC) is None
    s.enqueue(JobKind.LIBRARY_SYNC, triggered_by="schedule")
    when = s.last_created(JobKind.LIBRARY_SYNC)
    assert when is not None and when.tzinfo is not None
    assert s.last_created(JobKind.DEDUP_SCAN) is None  # per kind


def test_reap_running_fails_or_requeues_by_kind(tmp_path: Path) -> None:
    s = _store(tmp_path)
    sync, _ = s.enqueue(JobKind.LIBRARY_SYNC, triggered_by="schedule")
    scan, _ = s.enqueue(JobKind.DEDUP_SCAN, triggered_by="schedule")
    s.claim_next("sync")
    s.claim_next("scan")
    assert s.reap_running() == (1, 1)
    failed = s.get(sync)
    assert failed is not None and failed["status"] == "failed"
    assert failed["error"] == "interrupted by restart"
    resumed = s.claim_next("scan")  # the incremental scan picks up again
    assert resumed is not None and resumed.id == scan


def _age(s: SqliteJobStore, job_id: str, days: int) -> None:
    old = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    s._conn.execute(
        "UPDATE jobs SET created_at=?, finished_at=CASE WHEN finished_at IS NULL THEN NULL "
        "ELSE ? END WHERE id=?",
        (old, old, job_id),
    )


def test_prune_drops_old_finished_jobs_and_their_events(tmp_path: Path) -> None:
    s = _store(tmp_path)
    old, _ = s.enqueue(JobKind.HISTORY_IMPORT, triggered_by="manual")
    s.finish(old, JobStatus.DONE)
    _age(s, old, 40)
    recent, _ = s.enqueue(JobKind.HISTORY_IMPORT, triggered_by="manual")
    s.finish(recent, JobStatus.DONE)
    stuck, _ = s.enqueue(JobKind.DEDUP_SCAN, triggered_by="manual")  # queued, ancient
    _age(s, stuck, 400)

    assert s.prune(30) == 1
    assert s.get(old) is None and s.events(old) == []  # events cascade
    assert s.get(recent) is not None
    assert s.get(stuck) is not None  # queued/running are never pruned


def test_prune_honours_per_kind_overrides(tmp_path: Path) -> None:
    s = _store(tmp_path)
    poll, _ = s.enqueue(JobKind.LISTEN_POLL, triggered_by="schedule")
    s.finish(poll, JobStatus.DONE)
    _age(s, poll, 10)
    assert s.prune(30) == 0  # inside the default window
    assert s.prune(30, overrides={JobKind.LISTEN_POLL: 7}) == 1


def test_enqueue_with_unknown_user_raises_not_masked(tmp_path: Path) -> None:
    s = _store(tmp_path)
    with pytest.raises(sqlite3.IntegrityError):
        s.enqueue(JobKind.HISTORY_IMPORT, triggered_by="manual", user_id="ghost")
    with pytest.raises(sqlite3.IntegrityError):  # single-flight path must not swallow an FK error
        s.enqueue(JobKind.LIBRARY_SYNC, triggered_by="manual", user_id="ghost")
