"""Request history store — the :class:`RequestRecorder` implementation + reads.

Writers (the cascade running in shim threads) and readers (the console process)
use separate connections to the same WAL DB. Within a writer process several
download threads share one connection, so writes are serialised by a lock;
reads run in the console process and never contend with it.
"""

from __future__ import annotations

import sqlite3
import threading
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from crate.application.download import DownloadReport
from crate.domain.identity import TrackIdentity
from crate.domain.matching import norm_tokens
from crate.infrastructure.sqlite.db import now_iso

_STATS_TTL_S = 2.0  # console SSE polls stats every 2 s per client; cache the frame


def _new_id() -> str:
    return uuid.uuid4().hex


def _identity_cols(
    identity: TrackIdentity | None,
) -> tuple[str | None, str | None, str | None, int | None]:
    """The four request identity columns (artist, title, isrc, duration_s)."""
    if identity is None:
        return None, None, None, None
    return identity.artist, identity.title, identity.isrc, identity.duration_s


def _row_identity(row: Any) -> TrackIdentity:
    """Reconstruct a TrackIdentity from a row that selected the identity columns."""
    return TrackIdentity(
        artist=row["artist"],
        title=row["title"],
        isrc=row["isrc"],
        duration_s=row["duration_s"],
    )


class RequestStore:
    def __init__(
        self,
        conn: sqlite3.Connection,
        requested_via: str = "api",
        *,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self._conn = conn
        self._via = requested_via
        self._lock = threading.Lock()
        self._now = now
        # cached stats() frame + the monotonic time it was computed; guarded by
        # self._lock. Every write busts it via _invalidate_stats() so a reader
        # never serves a frame that predates its own write.
        self._stats_cache: tuple[dict[str, Any], float] | None = None

    def _invalidate_stats(self) -> None:
        """Drop the cached stats frame. MUST be called while holding self._lock
        (every write method does), so the invalidation is atomic with the write."""
        self._stats_cache = None

    # --- RequestRecorder (writes) ---
    def open_request(self, query: str, identity: TrackIdentity | None = None) -> str:
        rid = _new_id()
        artist, title, isrc, duration_s = _identity_cols(identity)
        with self._lock:
            self._conn.execute(
                "INSERT INTO requests"
                "(id, query, requested_via, status, created_at, artist, title, isrc, duration_s) "
                "VALUES (?, ?, ?, 'downloading', ?, ?, ?, ?, ?)",
                (rid, query, self._via, now_iso(), artist, title, isrc, duration_s),
            )
            self._invalidate_stats()
        return rid

    # --- console queue (cross-process work items) ---
    def enqueue(
        self, query: str, batch_id: str | None = None, identity: TrackIdentity | None = None
    ) -> str:
        """Record a console-requested download as ``queued``; the shim's queue
        worker claims and runs it. ``batch_id`` groups it into a bulk batch;
        ``identity`` persists the structured artist/title/ISRC/duration columns.
        Returns the new request id."""
        rid = _new_id()
        artist, title, isrc, duration_s = _identity_cols(identity)
        with self._lock:
            self._conn.execute(
                "INSERT INTO requests"
                "(id, query, requested_via, status, created_at, batch_id, "
                "artist, title, isrc, duration_s) "
                "VALUES (?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?)",
                (rid, query, self._via, now_iso(), batch_id, artist, title, isrc, duration_s),
            )
            self._invalidate_stats()
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
        Returns ``{"id", "query", "identity"}`` or ``None`` when nothing is
        claimable; ``identity`` is the row's :class:`TrackIdentity`."""
        with self._lock:
            row = self._conn.execute(
                "SELECT id, query, artist, title, isrc, duration_s FROM requests "
                f"WHERE {self._CLAIMABLE} ORDER BY created_at LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            cur = self._conn.execute(
                "UPDATE requests SET status='downloading' WHERE id=? AND status='queued'",
                (row["id"],),
            )
            if cur.rowcount != 1:  # another worker claimed it first
                return None
            self._invalidate_stats()  # a row moved queued -> downloading
            return {"id": row["id"], "query": row["query"], "identity": _row_identity(row)}

    def record_attempt(
        self, request_id: str, source: str, tier: int, ok: bool, detail: str
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO attempts(request_id, source, tier, ok, detail, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (request_id, source, tier, 1 if ok else 0, detail, now_iso()),
            )
            self._invalidate_stats()

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
            self._invalidate_stats()

    def fail_request(self, request_id: str, error: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE requests SET status='failed', error=?, finished_at=? WHERE id=?",
                (error, now_iso(), request_id),
            )
            self._invalidate_stats()

    # --- reads (console) ---
    # NB: all reads take self._lock too. The console shares one SQLite connection
    # across uvicorn's threadpool + the SSE thread; without serialising, concurrent
    # cursors on the same connection interleave and return rows from the wrong query.
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
        with self._lock:
            return [dict(row) for row in self._conn.execute(sql, params)]

    def get_request(self, request_id: str) -> dict[str, Any] | None:
        with self._lock:
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
        """Aggregate dashboard counters. Cached for ``_STATS_TTL_S`` (the SSE poll
        cadence) and busted by every write, so the ~10 full-table aggregates below
        run at most once per TTL no matter how many console clients poll."""
        conn = self._conn

        def scalar(sql: str) -> Any:
            row = conn.execute(sql).fetchone()
            return row[0] if row and row[0] is not None else 0

        with self._lock:
            cached = self._stats_cache
            if cached is not None and self._now() - cached[1] < _STATS_TTL_S:
                return cached[0]
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
            total_bytes = scalar("SELECT COALESCE(SUM(size_bytes), 0) FROM downloads")
            avg_elapsed_s = scalar("SELECT COALESCE(AVG(elapsed_s), 0) FROM downloads")
            frame = {
                "total": total,
                "done": done,
                "failed": failed,
                "in_flight": in_flight,
                "queued": queued,
                "success_rate": (done / settled) if settled else 0.0,
                "lossless": lossless,
                "lossless_rate": (lossless / done) if done else 0.0,
                "total_bytes": total_bytes,
                "avg_elapsed_s": avg_elapsed_s,
                "wins_by_source": wins_by_source,
                "wins_by_tier": wins_by_tier,
                "attempts_by_source": attempts_by_source,
            }
            self._stats_cache = (frame, self._now())
            return frame

    # --- bulk batches ---
    def create_batch(self, name: str, origin: str, owner: str, total: int) -> str:
        bid = _new_id()
        with self._lock:
            self._conn.execute(
                "INSERT INTO batches(id, name, origin, owner, total, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (bid, name, origin, owner, total, now_iso()),
            )
            self._invalidate_stats()
        return bid

    def list_batches(self) -> list[dict[str, Any]]:
        """Batches with per-status counts (newest first)."""
        with self._lock:
            rows = list(
                self._conn.execute(
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
        with self._lock:
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
            self._invalidate_stats()
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
            self._invalidate_stats()
        return cur.rowcount == 1

    def reap_orphans(self) -> int:
        """Recover rows a dead process left mid-flight: reset every
        ``downloading`` request back to ``queued`` so the queue workers re-claim
        it. Called once at ``serve`` startup, before any worker starts — the
        serve process owns the only writer, so any ``downloading`` row at that
        point is definitionally orphaned (its worker died on a crash/restart).
        Without this, ``claim_next`` (which only picks ``queued``) never touches
        them and they hang forever. Returns the number requeued."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE requests SET status='queued', finished_at=NULL WHERE status='downloading'"
            )
            self._invalidate_stats()
        return cur.rowcount

    def requeue(self, request_id: str) -> bool:
        """Retry a failed (or cancelled) request: reset it to ``queued``."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE requests SET status='queued', error=NULL, finished_at=NULL "
                "WHERE id=? AND status IN ('failed', 'cancelled')",
                (request_id,),
            )
            self._invalidate_stats()
        return cur.rowcount == 1

    # --- de-duplication (skip re-downloading a track already in the library) ---
    _COMPLETED_SELECT = (
        "SELECT r.tier_won, r.source_won, r.artist, r.title, r.isrc, d.* FROM requests r "
        "JOIN downloads d ON d.request_id = r.id WHERE r.status = 'done'"
    )

    def find_completed(self, identity_or_query: TrackIdentity | str) -> dict[str, Any] | None:
        """The most recent successful download matching ``identity_or_query``.

        Matching order (first hit wins): exact ISRC, then normalized-token
        equality on artist+title, then legacy exact-query text. Returns the
        winner (tier/source) + download row, whatever its quality — the CALLER
        decides whether to reuse (lossless only, per the upgrade path) or upgrade.
        ``None`` when nothing completed matches."""
        if isinstance(identity_or_query, str):
            identity: TrackIdentity | None = None
            query = identity_or_query
        else:
            identity = identity_or_query
            query = identity.display()
        with self._lock:
            if identity is not None and identity.isrc:
                row = self._conn.execute(
                    f"{self._COMPLETED_SELECT} AND r.isrc = ? ORDER BY r.finished_at DESC LIMIT 1",
                    (identity.isrc,),
                ).fetchone()
                if row is not None:
                    return dict(row)
            if identity is not None and (identity.artist or identity.title):
                row = self._token_match(identity)
                if row is not None:
                    return dict(row)
            if query:
                row = self._conn.execute(
                    f"{self._COMPLETED_SELECT} AND r.query = ? ORDER BY r.finished_at DESC LIMIT 1",
                    (query,),
                ).fetchone()
                if row is not None:
                    return dict(row)
        return None

    def _token_match(self, identity: TrackIdentity) -> Any | None:
        """Newest completed row whose artist+title normalize to the same tokens as
        ``identity``. Called while holding ``self._lock``. Compares in Python (the
        token normalizer is not expressible in SQL) over the completed rows that
        carry structured artist/title, newest first."""
        want = norm_tokens(identity.artist or "") + norm_tokens(identity.title or "")
        if not want:
            return None
        rows = self._conn.execute(
            f"{self._COMPLETED_SELECT} AND (r.artist IS NOT NULL OR r.title IS NOT NULL) "
            "ORDER BY r.finished_at DESC"
        )
        for row in rows:
            got = norm_tokens(row["artist"] or "") + norm_tokens(row["title"] or "")
            if got == want:
                return row
        return None

    def complete_as_duplicate(self, request_id: str, existing: dict[str, Any]) -> None:
        """Resolve a request as done from an existing library file — no download.
        Copies the prior download row (elapsed 0) and marks it done."""
        with self._lock:
            self._conn.execute(
                "UPDATE requests SET status='done', tier_won=?, source_won=?, finished_at=? "
                "WHERE id=?",
                (existing.get("tier_won"), existing.get("source_won"), now_iso(), request_id),
            )
            self._conn.execute(
                "INSERT OR REPLACE INTO downloads(request_id, path, size_bytes, codec, bitrate, "
                "bit_depth, sample_rate, duration_s, elapsed_s, lossless, quality_label) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)",
                (
                    request_id,
                    existing["path"],
                    existing["size_bytes"],
                    existing["codec"],
                    existing["bitrate"],
                    existing["bit_depth"],
                    existing["sample_rate"],
                    existing["duration_s"],
                    existing["lossless"],
                    existing["quality_label"],
                ),
            )
            self._invalidate_stats()

    # --- maintenance (retention) ---
    def prune_attempts(self, older_than_days: int = 90) -> int:
        """Delete attempt rows older than ``older_than_days``; return the count.

        The ``attempts`` table grows unbounded (one row per source tried per
        request); it's only used for the per-request trail + the stats aggregate,
        so old rows carry no value. ``created_at`` is a UTC ISO-8601 string, which
        sorts lexicographically the same as chronologically, so a plain string
        comparison against the computed cutoff is a correct range predicate."""
        cutoff = (datetime.now(UTC) - timedelta(days=older_than_days)).isoformat()
        with self._lock:
            cur = self._conn.execute("DELETE FROM attempts WHERE created_at < ?", (cutoff,))
            self._invalidate_stats()
        return cur.rowcount
