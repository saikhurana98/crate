"""Request history store — the :class:`RequestRecorder` implementation + reads.

Writers (the cascade running in shim threads) and readers (the console process)
use separate connections to the same WAL DB. Within a writer process several
download threads share one connection, so writes are serialised by a lock;
reads run in the console process and never contend with it.
"""

from __future__ import annotations

import sqlite3
import threading
import uuid
from typing import Any

from crate.application.download import DownloadReport
from crate.infrastructure.sqlite.db import now_iso


def _new_id() -> str:
    return uuid.uuid4().hex


class RequestStore:
    def __init__(self, conn: sqlite3.Connection, requested_via: str = "api") -> None:
        self._conn = conn
        self._via = requested_via
        self._lock = threading.Lock()

    # --- RequestRecorder (writes) ---
    def open_request(self, query: str) -> str:
        rid = _new_id()
        with self._lock:
            self._conn.execute(
                "INSERT INTO requests(id, query, requested_via, status, created_at) "
                "VALUES (?, ?, ?, 'downloading', ?)",
                (rid, query, self._via, now_iso()),
            )
        return rid

    # --- console queue (cross-process work items) ---
    def enqueue(self, query: str, batch_id: str | None = None) -> str:
        """Record a console-requested download as ``queued``; the shim's queue
        worker claims and runs it. ``batch_id`` groups it into a bulk batch.
        Returns the new request id."""
        rid = _new_id()
        with self._lock:
            self._conn.execute(
                "INSERT INTO requests(id, query, requested_via, status, created_at, batch_id) "
                "VALUES (?, ?, ?, 'queued', ?, ?)",
                (rid, query, self._via, now_iso(), batch_id),
            )
        return rid

    # requests in a paused or cancelled batch are not claimable
    _CLAIMABLE = (
        "status='queued' AND (batch_id IS NULL OR batch_id NOT IN "
        "(SELECT id FROM batches WHERE paused=1 OR cancelled=1))"
    )

    def claim_next(self) -> dict[str, Any] | None:
        """Atomically claim the oldest claimable ``queued`` request
        (-> ``downloading``), skipping paused/cancelled batches. Safe under
        concurrent claimers (the guarded UPDATE makes claiming exclusive).
        Returns ``{"id", "query"}`` or ``None`` when nothing is claimable."""
        with self._lock:
            row = self._conn.execute(
                f"SELECT id, query FROM requests WHERE {self._CLAIMABLE} "
                "ORDER BY created_at LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            cur = self._conn.execute(
                "UPDATE requests SET status='downloading' WHERE id=? AND status='queued'",
                (row["id"],),
            )
            if cur.rowcount != 1:  # another worker claimed it first
                return None
            return {"id": row["id"], "query": row["query"]}

    def record_attempt(
        self, request_id: str, source: str, tier: int, ok: bool, detail: str
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO attempts(request_id, source, tier, ok, detail, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (request_id, source, tier, 1 if ok else 0, detail, now_iso()),
            )

    def finish_request(self, request_id: str, report: DownloadReport) -> None:
        outcome, info, verdict = report.outcome, report.info, report.verdict
        if outcome is None or info is None or verdict is None:  # defensive; a winner has all three
            return
        with self._lock:
            self._conn.execute(
                "UPDATE requests SET status='done', tier_won=?, source_won=?, finished_at=? "
                "WHERE id=?",
                (outcome.tier, outcome.source, now_iso(), request_id),
            )
            self._conn.execute(
                "INSERT OR REPLACE INTO downloads(request_id, path, size_bytes, codec, bitrate, "
                "bit_depth, sample_rate, duration_s, elapsed_s, lossless, quality_label) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    request_id,
                    str(outcome.path),
                    info.size_bytes,
                    info.codec,
                    info.bitrate_bps,
                    info.bit_depth,
                    info.sample_rate_hz,
                    info.duration_s,
                    outcome.elapsed_s,
                    1 if verdict.lossless else 0,
                    verdict.label,
                ),
            )

    def fail_request(self, request_id: str, error: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE requests SET status='failed', error=?, finished_at=? WHERE id=?",
                (error, now_iso(), request_id),
            )

    # --- reads (console) ---
    def list_requests(
        self,
        status: str | None = None,
        query: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        sql = (
            "SELECT r.*, d.quality_label, d.size_bytes, d.lossless, d.elapsed_s "
            "FROM requests r LEFT JOIN downloads d ON d.request_id = r.id"
        )
        clauses: list[str] = []
        params: list[Any] = []
        if status:
            clauses.append("r.status = ?")
            params.append(status)
        if query:
            clauses.append("r.query LIKE ?")
            params.append(f"%{query}%")
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY r.created_at DESC LIMIT ? OFFSET ?"
        params += [limit, offset]
        return [dict(row) for row in self._conn.execute(sql, params)]

    def get_request(self, request_id: str) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM requests WHERE id=?", (request_id,)).fetchone()
        if row is None:
            return None
        out = dict(row)
        out["attempts"] = [
            dict(a)
            for a in self._conn.execute(
                "SELECT * FROM attempts WHERE request_id=? ORDER BY id", (request_id,)
            )
        ]
        dl = self._conn.execute(
            "SELECT * FROM downloads WHERE request_id=?", (request_id,)
        ).fetchone()
        out["download"] = dict(dl) if dl else None
        return out

    def stats(self) -> dict[str, Any]:
        conn = self._conn

        def scalar(sql: str) -> Any:
            row = conn.execute(sql).fetchone()
            return row[0] if row and row[0] is not None else 0

        total = scalar("SELECT COUNT(*) FROM requests")
        done = scalar("SELECT COUNT(*) FROM requests WHERE status='done'")
        failed = scalar("SELECT COUNT(*) FROM requests WHERE status='failed'")
        in_flight = scalar("SELECT COUNT(*) FROM requests WHERE status='downloading'")
        queued = scalar("SELECT COUNT(*) FROM requests WHERE status='queued'")
        lossless = scalar("SELECT COUNT(*) FROM downloads WHERE lossless=1")
        settled = done + failed
        wins_by_source = {
            row["source_won"]: row["n"]
            for row in conn.execute(
                "SELECT source_won, COUNT(*) n FROM requests "
                "WHERE source_won IS NOT NULL GROUP BY source_won"
            )
        }
        wins_by_tier = {
            str(row["tier_won"]): row["n"]
            for row in conn.execute(
                "SELECT tier_won, COUNT(*) n FROM requests "
                "WHERE tier_won IS NOT NULL GROUP BY tier_won"
            )
        }
        attempts_by_source = {
            row["source"]: {"ok": row["ok"], "total": row["n"]}
            for row in conn.execute(
                "SELECT source, SUM(ok) ok, COUNT(*) n FROM attempts GROUP BY source"
            )
        }
        return {
            "total": total,
            "done": done,
            "failed": failed,
            "in_flight": in_flight,
            "queued": queued,
            "success_rate": (done / settled) if settled else 0.0,
            "lossless": lossless,
            "lossless_rate": (lossless / done) if done else 0.0,
            "total_bytes": scalar("SELECT COALESCE(SUM(size_bytes), 0) FROM downloads"),
            "avg_elapsed_s": scalar("SELECT COALESCE(AVG(elapsed_s), 0) FROM downloads"),
            "wins_by_source": wins_by_source,
            "wins_by_tier": wins_by_tier,
            "attempts_by_source": attempts_by_source,
        }

    # --- bulk batches ---
    def create_batch(self, name: str, origin: str, owner: str, total: int) -> str:
        bid = _new_id()
        with self._lock:
            self._conn.execute(
                "INSERT INTO batches(id, name, origin, owner, total, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (bid, name, origin, owner, total, now_iso()),
            )
        return bid

    def list_batches(self) -> list[dict[str, Any]]:
        """Batches with per-status counts (newest first)."""
        rows = self._conn.execute(
            "SELECT b.*, "
            "COUNT(r.id) AS n, "
            "COALESCE(SUM(r.status='done'), 0) AS done, "
            "COALESCE(SUM(r.status='failed'), 0) AS failed, "
            "COALESCE(SUM(r.status='downloading'), 0) AS downloading, "
            "COALESCE(SUM(r.status='queued'), 0) AS queued, "
            "COALESCE(SUM(r.status='cancelled'), 0) AS cancelled "
            "FROM batches b LEFT JOIN requests r ON r.batch_id = b.id "
            "GROUP BY b.id ORDER BY b.created_at DESC"
        )
        out: list[dict[str, Any]] = []
        for row in rows:
            d = dict(row)
            d["paused"] = bool(d["paused"])
            d["cancelled_flag"] = bool(d.pop("cancelled"))  # batch flag vs the count column
            d["counts"] = {
                "done": d.pop("done"),
                "failed": d.pop("failed"),
                "downloading": d.pop("downloading"),
                "queued": d.pop("queued"),
            }
            out.append(d)
        return out

    def get_batch(self, batch_id: str) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
        if row is None:
            return None
        out = dict(row)
        out["tracks"] = [
            dict(t)
            for t in self._conn.execute(
                "SELECT id, query, status, source_won, tier_won, error "
                "FROM requests WHERE batch_id=? ORDER BY created_at",
                (batch_id,),
            )
        ]
        return out

    def set_batch_paused(self, batch_id: str, paused: bool) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE batches SET paused=? WHERE id=?", (1 if paused else 0, batch_id)
            )
        return cur.rowcount == 1

    def cancel_batch(self, batch_id: str) -> bool:
        """Flag the batch cancelled and drop its still-queued requests. Requests
        already downloading are left to finish."""
        with self._lock:
            cur = self._conn.execute("UPDATE batches SET cancelled=1 WHERE id=?", (batch_id,))
            self._conn.execute(
                "UPDATE requests SET status='cancelled' WHERE batch_id=? AND status='queued'",
                (batch_id,),
            )
        return cur.rowcount == 1

    def requeue(self, request_id: str) -> bool:
        """Retry a failed (or cancelled) request: reset it to ``queued``."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE requests SET status='queued', error=NULL, finished_at=NULL "
                "WHERE id=? AND status IN ('failed', 'cancelled')",
                (request_id,),
            )
        return cur.rowcount == 1
