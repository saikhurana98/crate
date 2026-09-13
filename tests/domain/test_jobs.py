"""Jobs vocabulary + the scheduling rule (pure)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from crate.domain.jobs import (
    LANE_OF,
    RESUMABLE,
    SINGLE_FLIGHT,
    TERMINAL,
    JobKind,
    JobStatus,
    Lane,
    schedule_due,
)

NOW = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)


def test_schedule_due_when_never_run() -> None:
    assert schedule_due(None, 7200, NOW) is True


def test_schedule_not_due_inside_interval() -> None:
    assert schedule_due(NOW - timedelta(seconds=7199), 7200, NOW) is False


def test_schedule_due_at_and_after_interval() -> None:
    assert schedule_due(NOW - timedelta(seconds=7200), 7200, NOW) is True
    assert schedule_due(NOW - timedelta(days=3), 7200, NOW) is True  # one run, not a burst


def test_every_kind_has_a_lane() -> None:
    assert set(LANE_OF) == set(JobKind)


def test_scan_and_mutations_never_share_the_sync_lane() -> None:
    assert LANE_OF[JobKind.LIBRARY_SYNC] is Lane.SYNC
    assert LANE_OF[JobKind.DEDUP_SCAN] is Lane.SCAN  # a long scan can't block the sync
    assert {LANE_OF[k] for k in (JobKind.DEDUP_PURGE, JobKind.TRASH_RESTORE)} == {Lane.MUTATE}


def test_single_flight_resumable_and_terminal_sets() -> None:
    assert JobKind.LIBRARY_SYNC in SINGLE_FLIGHT
    assert JobKind.HISTORY_IMPORT not in SINGLE_FLIGHT  # several uploads may queue
    assert {JobKind.DEDUP_SCAN} == RESUMABLE
    assert {JobStatus.DONE, JobStatus.FAILED, JobStatus.CANCELLED} == TERMINAL
