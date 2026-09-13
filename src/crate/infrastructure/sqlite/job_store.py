"""SQLite job queue + event log for the background-jobs engine.

Same discipline as :class:`RequestStore`: one WAL connection per store, every
method under a lock. Claiming uses the guarded UPDATE (``... WHERE id=? AND
status='queued'``), so concurrent runners, even in different processes, never
double-claim. Single-flight kinds rely on the partial unique index
``uq_jobs_active``: a racing "Run now" and scheduler tick get one job.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from crate.application.jobs import Job
from crate.domain.jobs import LANE_OF, RESUMABLE, SINGLE_FLIGHT, TERMINAL, JobKind, JobStatus
from crate.infrastructure.sqlite.db import now_iso

_EVENT_COLS = "id, job_id, ts, level, code, message, user_id, data_json"


def _loads(raw: str | None) -> Any:
    return json.loads(raw) if raw else None


def _job_dict(row: sqlite3.Row) -> dict[str, Any]:
    out = dict(row)
    out["params"] = _loads(out.pop("params_json")) or {}
    out["summary"] = _loads(out.pop("summary_json"))
    out["cancel_requested"] = bool(out["cancel_requested"])
    out.pop("dedupe_key", None)
    return out


def _event_dict(row: sqlite3.Row) -> dict[str, Any]:
    out = dict(row)
    out["data"] = _loads(out.pop("data_json"))
    return out


class SqliteJobStore:
    def __init__(self, conn: sqlite3.Connection, *, lock: threading.Lock | None = None) -> None:
        self._conn = conn
        self._lock = lock or threading.Lock()

    # --- queue ---
    def enqueue(
        self,
        kind: JobKind,
        *,
        triggered_by: str,
        user_id: str | None = None,
        params: dict[str, Any] | None = None,
    ) -> tuple[str, bool]:
        """Queue a job; return ``(job_id, existing)``. A single-flight kind that's
        already queued or running returns *that* job (``existing=True``)."""
        jid = uuid.uuid4().hex
        dedupe = kind.value if kind in SINGLE_FLIGHT else None
        with self._lock:
            try:
                self._conn.execute(
                    "INSERT INTO jobs(id, kind, lane, status, triggered_by, user_id, "
                    "dedupe_key, params_json, created_at) "
                    "VALUES (?, ?, ?, 'queued', ?, ?, ?, ?, ?)",
                    (
                        jid,
                        kind.value,
                        LANE_OF[kind].value,
                        triggered_by,
                        user_id,
                        dedupe,
                        json.dumps(params or {}),
                        now_iso(),
                    ),
                )
            except sqlite3.IntegrityError:
                if dedupe is None:
                    raise
                row = self._conn.execute(
                    "SELECT id FROM jobs WHERE dedupe_key=? AND status IN ('queued', 'running')",
                    (dedupe,),
                ).fetchone()
                if row is None:  # not the single-flight index (e.g. unknown user FK)
                    raise
                return str(row["id"]), True
            self._insert_event(jid, "status", f"{kind.value} queued ({triggered_by})", "queued")
        return jid, False

    def claim_next(self, lane: str) -> Job | None:
        """Atomically claim the oldest queued job in ``lane`` (-> ``running``)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM jobs WHERE lane=? AND status='queued' ORDER BY created_at LIMIT 1",
                (lane,),
            ).fetchone()
            if row is None:
                return None
            now = now_iso()
            cur = self._conn.execute(
                "UPDATE jobs SET status='running', started_at=?, heartbeat_at=? "
                "WHERE id=? AND status='queued'",
                (now, now, row["id"]),
            )
            if cur.rowcount != 1:  # another runner won the race
                return None
            return Job(
                id=str(row["id"]),
                kind=JobKind(row["kind"]),
                lane=str(row["lane"]),
                status=JobStatus.RUNNING,
                triggered_by=str(row["triggered_by"]),
                user_id=row["user_id"],
                params=_loads(row["params_json"]) or {},
            )

    # --- the JobStore surface a running job drives ---
    def _insert_event(
        self,
        job_id: str,
        level: str,
        message: str,
        code: str | None = None,
        user_id: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> int:
        """Insert one event row. The caller must hold ``self._lock``."""
        cur = self._conn.execute(
            "INSERT INTO job_events(job_id, ts, level, code, message, user_id, data_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                job_id,
                now_iso(),
                level,
                code,
                message,
                user_id,
                json.dumps(data) if data is not None else None,
            ),
        )
        return int(cur.lastrowid or 0)

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
        with self._lock:
            return self._insert_event(job_id, level, message, code, user_id, data)

    def set_progress(self, job_id: str, done: int, total: int | None) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE jobs SET progress_done=?, progress_total=?, heartbeat_at=? WHERE id=?",
                (done, total, now_iso(), job_id),
            )

    def heartbeat(self, job_id: str) -> None:
        with self._lock:
            self._conn.execute("UPDATE jobs SET heartbeat_at=? WHERE id=?", (now_iso(), job_id))

    def finish(
        self,
        job_id: str,
        status: JobStatus,
        *,
        summary: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE jobs SET status=?, summary_json=?, error=?, finished_at=? WHERE id=?",
                (
                    status.value,
                    json.dumps(summary) if summary is not None else None,
                    error,
                    now_iso(),
                    job_id,
                ),
            )

    def cancel_requested(self, job_id: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT cancel_requested FROM jobs WHERE id=?", (job_id,)
            ).fetchone()
        return bool(row and row["cancel_requested"])

    def request_cancel(self, job_id: str) -> str | None:
        """Cancel a job. A queued job is cancelled immediately; a running one is
        flagged and stops at its next ``check_cancelled``. Returns ``"cancelled"``
        / ``"cancelling"``, or ``None`` when missing or already finished."""
        with self._lock:
            row = self._conn.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None or JobStatus(row["status"]) in TERMINAL:
                return None
            if row["status"] == JobStatus.QUEUED:
                self._conn.execute(
                    "UPDATE jobs SET status='cancelled', finished_at=? WHERE id=?",
                    (now_iso(), job_id),
                )
                self._insert_event(job_id, "status", "cancelled before it started", "cancelled")
                return "cancelled"
            self._conn.execute("UPDATE jobs SET cancel_requested=1 WHERE id=?", (job_id,))
            self._insert_event(job_id, "status", "cancel requested", "cancel_requested")
            return "cancelling"

    # --- reads ---
    def get(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return _job_dict(row) if row else None

    def list_jobs(self, *, kind: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        sql = "SELECT * FROM jobs"
        params: list[Any] = []
        if kind:
            sql += " WHERE kind=?"
            params.append(kind)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            return [_job_dict(r) for r in self._conn.execute(sql, params)]

    def events(
        self, job_id: str, *, after: int = 0, limit: int = 500, viewer: str | None = None
    ) -> list[dict[str, Any]]:
        """One job's log after cursor ``after``. User-scoped lines are visible only
        to their user; global lines (``user_id`` NULL) to everyone."""
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {_EVENT_COLS} FROM job_events WHERE job_id=? AND id>? "
                "AND (user_id IS NULL OR user_id=?) ORDER BY id LIMIT ?",
                (job_id, after, viewer, limit),
            ).fetchall()
        return [_event_dict(r) for r in rows]

    def events_since(
        self, cursor: int, *, limit: int = 500, viewer: str | None = None
    ) -> list[dict[str, Any]]:
        """Every job's events after ``cursor``, oldest first — the live-log tail."""
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {_EVENT_COLS} FROM job_events WHERE id>? "
                "AND (user_id IS NULL OR user_id=?) ORDER BY id LIMIT ?",
                (cursor, viewer, limit),
            ).fetchall()
        return [_event_dict(r) for r in rows]

    def last_created(self, kind: JobKind) -> datetime | None:
        """When the newest job of ``kind`` was created (drives the scheduler)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT MAX(created_at) AS t FROM jobs WHERE kind=?", (kind.value,)
            ).fetchone()
        return datetime.fromisoformat(row["t"]) if row and row["t"] else None

    # --- lifecycle / maintenance ---
    def reap_running(self) -> tuple[int, int]:
        """Startup recovery: a job left ``running`` belongs to a dead process.
        Resumable kinds are re-queued; everything else fails with
        ``interrupted by restart``. Returns ``(failed, requeued)``."""
        failed = requeued = 0
        with self._lock:
            rows = self._conn.execute("SELECT id, kind FROM jobs WHERE status='running'").fetchall()
            for row in rows:
                if JobKind(row["kind"]) in RESUMABLE:
                    self._conn.execute(
                        "UPDATE jobs SET status='queued', started_at=NULL, cancel_requested=0 "
                        "WHERE id=?",
                        (row["id"],),
                    )
                    self._insert_event(
                        row["id"], "status", "re-queued after a restart (resumes)", "requeued"
                    )
                    requeued += 1
                else:
                    self._conn.execute(
                        "UPDATE jobs SET status='failed', error='interrupted by restart', "
                        "finished_at=? WHERE id=?",
                        (now_iso(), row["id"]),
                    )
                    self._insert_event(row["id"], "error", "interrupted by restart", "interrupted")
                    failed += 1
        return failed, requeued

    def prune(self, older_than_days: int, *, overrides: dict[JobKind, int] | None = None) -> int:
        """Delete *finished* jobs older than the retention window (their events go
        with them via ``ON DELETE CASCADE``). ``overrides`` sets a shorter/longer
        window per kind (e.g. listen_poll: 7 days). Queued/running jobs are never
        pruned. Returns the number of jobs deleted."""
        overrides = overrides or {}
        now = datetime.now(UTC)
        deleted = 0
        terminal = tuple(s.value for s in TERMINAL)
        marks = ",".join("?" * len(terminal))
        with self._lock:
            for kind in JobKind:
                days = overrides.get(kind, older_than_days)
                cutoff = (now - timedelta(days=days)).isoformat()
                cur = self._conn.execute(
                    f"DELETE FROM jobs WHERE kind=? AND status IN ({marks}) "
                    "AND COALESCE(finished_at, created_at) < ?",
                    (kind.value, *terminal, cutoff),
                )
                deleted += cur.rowcount
        return deleted
