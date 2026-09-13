"""Background-job vocabulary + the scheduling rule (pure) — the engine behind
Library Sync and Dedup.

Jobs run in ``serve``: the only process with ``/library`` read-write, and the one
that keeps running with no browser open. Work is split into **lanes**, one runner
thread each, so a multi-hour first dedup scan never holds up the 2-hourly library
sync, and destructive library mutations never run inside the scan lane.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum


class JobKind(StrEnum):
    LIBRARY_SYNC = "library_sync"
    LISTEN_POLL = "listen_poll"
    HISTORY_IMPORT = "history_import"
    DEDUP_SCAN = "dedup_scan"
    DEDUP_PURGE = "dedup_purge"
    TRASH_RESTORE = "trash_restore"
    TRASH_EMPTY = "trash_empty"


class Lane(StrEnum):
    SYNC = "sync"
    SCAN = "scan"
    MUTATE = "mutate"


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL = frozenset({JobStatus.DONE, JobStatus.FAILED, JobStatus.CANCELLED})

LANE_OF: dict[JobKind, Lane] = {
    JobKind.LIBRARY_SYNC: Lane.SYNC,
    JobKind.LISTEN_POLL: Lane.SYNC,
    JobKind.HISTORY_IMPORT: Lane.SYNC,
    JobKind.DEDUP_SCAN: Lane.SCAN,
    JobKind.DEDUP_PURGE: Lane.MUTATE,
    JobKind.TRASH_RESTORE: Lane.MUTATE,
    JobKind.TRASH_EMPTY: Lane.MUTATE,
}

# At most one queued-or-running job of these kinds at a time (enforced by the
# partial unique index uq_jobs_active): "Run now" during a scheduled run returns
# the job that's already going instead of starting a second one.
SINGLE_FLIGHT = frozenset(
    {JobKind.LIBRARY_SYNC, JobKind.LISTEN_POLL, JobKind.DEDUP_SCAN, JobKind.TRASH_EMPTY}
)

# Incremental kinds the startup reaper re-queues instead of failing: they resume
# where they stopped, so a restart mid-run costs nothing.
RESUMABLE = frozenset({JobKind.DEDUP_SCAN})


def schedule_due(last_created: datetime | None, interval_s: float, now: datetime) -> bool:
    """Whether a periodic job is due: never run before, or its last run was created
    at least ``interval_s`` ago. Keyed on the time stored in the DB, so a restart
    neither loses the schedule nor fires a burst of catch-up runs."""
    if last_created is None:
        return True
    return (now - last_created).total_seconds() >= interval_s
