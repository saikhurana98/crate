"""Jobs engine use-case: run one claimed job through its handler, with a
:class:`JobContext` that logs, reports progress, and honours cancellation.

Pure orchestration over the :class:`JobStore` Protocol (SQLite implementation:
``infrastructure/sqlite/job_store.py``). Composition owns the runner threads and
the handler registry; handlers are plain ``(job, ctx) -> summary`` callables.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from crate.domain.jobs import JobKind, JobStatus, schedule_due

# Progress rows are cheap to UPDATE but each event is a log line, so a 5k-file
# scan emits a progress event at most this often (plus the final one).
_PROGRESS_EVENT_EVERY_S = 1.0


@dataclass(frozen=True)
class Job:
    id: str
    kind: JobKind
    lane: str
    status: JobStatus
    triggered_by: str
    user_id: str | None = None
    params: dict[str, Any] = field(default_factory=dict)


class JobStore(Protocol):
    """The store surface a running job drives."""

    def add_event(
        self,
        job_id: str,
        level: str,
        message: str,
        *,
        code: str | None = None,
        user_id: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> int: ...

    def set_progress(self, job_id: str, done: int, total: int | None) -> None: ...

    def finish(
        self,
        job_id: str,
        status: JobStatus,
        *,
        summary: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None: ...

    def cancel_requested(self, job_id: str) -> bool: ...


class JobCancelled(Exception):
    """Raised by :meth:`JobContext.check_cancelled` to unwind a cancelled job."""


class JobContext:
    """What a handler gets: logging, throttled progress, and cancellation checks."""

    def __init__(
        self, job: Job, store: JobStore, *, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.job = job
        self._store = store
        self._clock = clock
        self._last_progress_event: float | None = None

    def log(
        self,
        message: str,
        *,
        level: str = "info",
        code: str | None = None,
        user_id: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> None:
        """Append a log line. ``user_id`` scopes it: only that user (and not other
        viewers) sees it — for lines that carry personal listening data."""
        self._store.add_event(self.job.id, level, message, code=code, user_id=user_id, data=data)

    def warn(self, message: str, **kw: Any) -> None:
        self.log(message, level="warn", **kw)

    def error(self, message: str, **kw: Any) -> None:
        self.log(message, level="error", **kw)

    def progress(self, done: int, total: int | None = None, message: str | None = None) -> None:
        """Persist progress on every call (a cheap UPDATE) but emit a progress
        *event* at most once a second, plus the final ``done == total`` one."""
        self._store.set_progress(self.job.id, done, total)
        now = self._clock()
        final = total is not None and done >= total
        last = self._last_progress_event
        if final or last is None or now - last >= _PROGRESS_EVENT_EVERY_S:
            self._last_progress_event = now
            text = message or (f"{done}/{total}" if total is not None else str(done))
            self._store.add_event(
                self.job.id, "progress", text, code="progress", data={"done": done, "total": total}
            )

    def check_cancelled(self) -> None:
        """Raise :class:`JobCancelled` if a cancel was requested for this job."""
        if self._store.cancel_requested(self.job.id):
            raise JobCancelled


Handler = Callable[[Job, JobContext], "dict[str, Any] | None"]


def run_claimed(
    job: Job,
    handlers: dict[JobKind, Handler],
    store: JobStore,
    *,
    clock: Callable[[], float] = time.monotonic,
) -> JobStatus:
    """Run a claimed (``running``) job to a terminal status and return it.

    Every transition is also written as a ``status`` event, so the live log only
    ever tails ``job_events``. A crashing handler fails its *job*, never the runner
    thread (store errors while finishing are the caller's to swallow)."""
    handler = handlers.get(job.kind)
    if handler is None:
        store.add_event(job.id, "error", f"no handler registered for {job.kind}", code="no_handler")
        store.finish(job.id, JobStatus.FAILED, error="no handler")
        return JobStatus.FAILED
    store.add_event(job.id, "status", f"{job.kind} started ({job.triggered_by})", code="started")
    ctx = JobContext(job, store, clock=clock)
    try:
        summary = handler(job, ctx) or {}
    except JobCancelled:
        store.add_event(job.id, "status", f"{job.kind} cancelled", code="cancelled")
        store.finish(job.id, JobStatus.CANCELLED)
        return JobStatus.CANCELLED
    except Exception as exc:
        store.add_event(job.id, "error", f"{job.kind} failed: {exc}", code="failed")
        store.finish(job.id, JobStatus.FAILED, error=str(exc))
        return JobStatus.FAILED
    store.add_event(job.id, "status", f"{job.kind} finished", code="finished", data=summary)
    store.finish(job.id, JobStatus.DONE, summary=summary)
    return JobStatus.DONE


@dataclass(frozen=True)
class Schedule:
    kind: JobKind
    interval_s: float


def due_kinds(
    schedules: Iterable[Schedule],
    last_created: Callable[[JobKind], datetime | None],
    now: datetime,
) -> list[JobKind]:
    """The scheduled kinds due to run now (see :func:`schedule_due`)."""
    return [s.kind for s in schedules if schedule_due(last_created(s.kind), s.interval_s, now)]
