"""run_claimed / JobContext / due_kinds against a fake store (no DB)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from crate.application.jobs import (
    Job,
    JobCancelled,
    JobContext,
    Schedule,
    due_kinds,
    run_claimed,
)
from crate.domain.jobs import JobKind, JobStatus


class FakeStore:
    def __init__(self) -> None:
        self.events: list[tuple[str, str | None, str, str | None, Any]] = []
        self.progress: list[tuple[int, int | None]] = []
        self.finished: tuple[JobStatus, Any, str | None] | None = None
        self.cancel = False

    def add_event(
        self,
        job_id: str,
        level: str,
        message: str,
        *,
        code: str | None = None,
        user_id: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> int:
        self.events.append((level, code, message, user_id, data))
        return len(self.events)

    def set_progress(self, job_id: str, done: int, total: int | None) -> None:
        self.progress.append((done, total))

    def finish(
        self,
        job_id: str,
        status: JobStatus,
        *,
        summary: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        self.finished = (status, summary, error)

    def cancel_requested(self, job_id: str) -> bool:
        return self.cancel


def _job(kind: JobKind = JobKind.LIBRARY_SYNC) -> Job:
    return Job(id="j1", kind=kind, lane="sync", status=JobStatus.RUNNING, triggered_by="manual")


def test_success_logs_transitions_and_summary() -> None:
    store = FakeStore()

    def handler(job: Job, ctx: JobContext) -> dict[str, Any]:
        ctx.log("checked 3 playlists")
        return {"playlists": 3}

    status = run_claimed(_job(), {JobKind.LIBRARY_SYNC: handler}, store)
    assert status is JobStatus.DONE
    assert [e[1] for e in store.events] == ["started", None, "finished"]
    assert store.events[-1][4] == {"playlists": 3}  # summary travels with the finish event
    assert store.finished == (JobStatus.DONE, {"playlists": 3}, None)


def test_handler_returning_none_gets_empty_summary() -> None:
    store = FakeStore()
    run_claimed(_job(), {JobKind.LIBRARY_SYNC: lambda job, ctx: None}, store)
    assert store.finished == (JobStatus.DONE, {}, None)


def test_crashing_handler_fails_the_job_not_the_runner() -> None:
    store = FakeStore()

    def boom(job: Job, ctx: JobContext) -> None:
        raise RuntimeError("spotify 503")

    assert run_claimed(_job(), {JobKind.LIBRARY_SYNC: boom}, store) is JobStatus.FAILED
    assert store.events[-1][:2] == ("error", "failed") and "spotify 503" in store.events[-1][2]
    assert store.finished == (JobStatus.FAILED, None, "spotify 503")


def test_missing_handler_fails_cleanly() -> None:
    store = FakeStore()
    assert run_claimed(_job(JobKind.DEDUP_SCAN), {}, store) is JobStatus.FAILED
    assert store.events[0][1] == "no_handler"
    assert store.finished == (JobStatus.FAILED, None, "no handler")


def test_cancellation_unwinds_to_cancelled() -> None:
    store = FakeStore()

    def slow(job: Job, ctx: JobContext) -> None:
        ctx.log("step 1")
        store.cancel = True  # user hit cancel mid-run
        ctx.check_cancelled()
        raise AssertionError("must not get here")

    assert run_claimed(_job(), {JobKind.LIBRARY_SYNC: slow}, store) is JobStatus.CANCELLED
    assert store.events[-1][1] == "cancelled"
    assert store.finished == (JobStatus.CANCELLED, None, None)


def test_check_cancelled_is_a_noop_when_not_requested() -> None:
    ctx = JobContext(_job(), FakeStore())
    ctx.check_cancelled()  # no raise


def test_progress_event_is_throttled_but_final_always_emitted() -> None:
    store = FakeStore()
    ticks = iter([0.0, 0.5, 1.2, 1.3])
    ctx = JobContext(_job(), store, clock=lambda: next(ticks))
    ctx.progress(1, 10)  # t=0.0 -> event (first)
    ctx.progress(2, 10)  # t=0.5 -> throttled
    ctx.progress(3, 10)  # t=1.2 -> event
    ctx.progress(10, 10)  # t=1.3 -> event (final, despite <1s)
    assert store.progress == [(1, 10), (2, 10), (3, 10), (10, 10)]  # every call persisted
    progress_events = [e for e in store.events if e[1] == "progress"]
    assert [e[2] for e in progress_events] == ["1/10", "3/10", "10/10"]
    assert progress_events[-1][4] == {"done": 10, "total": 10}


def test_progress_without_total_and_custom_message() -> None:
    store = FakeStore()
    ctx = JobContext(_job(), store, clock=lambda: 0.0)
    ctx.progress(7, message="fingerprinted 7 files")
    assert store.events[-1][2] == "fingerprinted 7 files"


def test_log_levels_and_user_scoping() -> None:
    store = FakeStore()
    ctx = JobContext(_job(), store)
    ctx.warn("rate limited", code="429")
    ctx.error("playlist gone", user_id="u1")
    assert store.events[0][:2] == ("warn", "429")
    assert store.events[1][0] == "error" and store.events[1][3] == "u1"


def test_due_kinds_picks_only_due_schedules() -> None:
    now = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
    last = {
        JobKind.LIBRARY_SYNC: now - timedelta(hours=3),  # due (2h interval)
        JobKind.LISTEN_POLL: now - timedelta(minutes=10),  # not due (30 min)
    }
    schedules = [
        Schedule(JobKind.LIBRARY_SYNC, 7200),
        Schedule(JobKind.LISTEN_POLL, 1800),
        Schedule(JobKind.DEDUP_SCAN, 86400),  # never run -> due
    ]
    assert due_kinds(schedules, last.get, now) == [JobKind.LIBRARY_SYNC, JobKind.DEDUP_SCAN]


def test_job_cancelled_is_an_exception() -> None:
    assert issubclass(JobCancelled, Exception)
