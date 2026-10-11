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

from crate.application.auth import SERVICE_USER_ID
from crate.application.download import DownloadReport
from crate.domain.candidate import Candidate
from crate.domain.identity import TrackIdentity
from crate.domain.listening import merge_key
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


def _delta_s(start_iso: str | None, end_iso: str | None) -> float | None:
    """Seconds between two UTC ISO-8601 timestamps, or ``None`` if either is
    missing/unparseable. Used to turn the feed's stored ``*_at`` marks into the
    two latencies (created -> first-playable, created -> final-FLAC)."""
    if not start_iso or not end_iso:
        return None
    try:
        delta = datetime.fromisoformat(end_iso) - datetime.fromisoformat(start_iso)
    except ValueError:
        return None
    return max(delta.total_seconds(), 0.0)


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
    def open_request(
        self, query: str, identity: TrackIdentity | None = None, *, user_id: str | None = None
    ) -> str:
        rid = _new_id()
        artist, title, isrc, duration_s = _identity_cols(identity)
        with self._lock:
            self._conn.execute(
                "INSERT INTO requests"
                "(id, query, requested_via, status, created_at, artist, title, isrc, duration_s, "
                "user_id) VALUES (?, ?, ?, 'downloading', ?, ?, ?, ?, ?, ?)",
                (
                    rid,
                    query,
                    self._via,
                    now_iso(),
                    artist,
                    title,
                    isrc,
                    duration_s,
                    user_id or SERVICE_USER_ID,
                ),
            )
            self._invalidate_stats()
        return rid

    # --- console queue (cross-process work items) ---
    def enqueue(
        self,
        query: str,
        batch_id: str | None = None,
        identity: TrackIdentity | None = None,
        *,
        user_id: str | None = None,
        priority: int = 0,
    ) -> str:
        """Record a console-requested download as ``queued``; the shim's queue
        worker claims and runs it. ``batch_id`` groups it into a bulk batch;
        ``identity`` persists the structured artist/title/ISRC/duration columns;
        ``user_id`` attributes it to the requesting user (defaults to the
        synthetic service user for automation/CLI). ``priority`` 1 marks auto-sync
        work, which yields to the same user's own requests. Returns the new request id."""
        rid = _new_id()
        artist, title, isrc, duration_s = _identity_cols(identity)
        with self._lock:
            self._conn.execute(
                "INSERT INTO requests"
                "(id, query, requested_via, status, created_at, batch_id, "
                "artist, title, isrc, duration_s, user_id, priority) "
                "VALUES (?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    rid,
                    query,
                    self._via,
                    now_iso(),
                    batch_id,
                    artist,
                    title,
                    isrc,
                    duration_s,
                    user_id or SERVICE_USER_ID,
                    priority,
                ),
            )
            self._invalidate_stats()
        return rid

    # requests in a paused or cancelled batch are not claimable
    _CLAIMABLE = (
        "status='queued' AND (batch_id IS NULL OR batch_id NOT IN "
        "(SELECT id FROM batches WHERE paused=1 OR cancelled=1))"
    )

    def claim_next(self) -> dict[str, Any] | None:
        """Atomically claim the next claimable ``queued`` request
        (-> ``downloading``), skipping paused/cancelled batches. Safe under
        concurrent claimers (the guarded UPDATE makes claiming exclusive).

        **Fair scheduling:** serve the user whose last claim is oldest
        (round-robin across users, NULLS-FIRST for never-served users), then FIFO
        within that user — so one user's 500-track import can't starve everyone
        else. Within a user, their own requests (priority 0) go before auto-sync
        (priority 1). Records the claim in ``owner_claims`` inside the same locked txn.
        Returns ``{"id", "query", "identity"}`` or ``None`` when nothing is
        claimable; ``identity`` is the row's :class:`TrackIdentity`."""
        with self._lock:
            row = self._conn.execute(
                "SELECT r.id, r.query, r.artist, r.title, r.isrc, r.duration_s, r.user_id "
                "FROM requests r LEFT JOIN owner_claims oc ON oc.user_id = r.user_id "
                f"WHERE {self._CLAIMABLE} "
                "ORDER BY COALESCE(oc.last_claim_at, '') ASC, r.priority ASC, r.created_at ASC "
                "LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            cur = self._conn.execute(
                "UPDATE requests SET status='downloading' WHERE id=? AND status='queued'",
                (row["id"],),
            )
            if cur.rowcount != 1:  # another worker claimed it first
                return None
            self._conn.execute(
                "INSERT INTO owner_claims(user_id, last_claim_at) VALUES (?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET last_claim_at=excluded.last_claim_at",
                (row["user_id"] or SERVICE_USER_ID, now_iso()),
            )
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
        now = now_iso()
        with self._lock:
            # first_playable_at is COALESCE-once: the first landing (usually the
            # lossy rip) sets it and a later lossless upgrade — which re-enters this
            # method via dedup._upgrade — must not overwrite it. final_flac_at is
            # set only on a lossless landing, so the two columns bracket the split.
            self._conn.execute(
                "UPDATE requests SET status='done', tier_won=?, source_won=?, finished_at=?, "
                "first_playable_at=COALESCE(first_playable_at, ?), "
                "final_flac_at=CASE WHEN ? THEN ? ELSE final_flac_at END "
                "WHERE id=?",
                (
                    outcome.tier,
                    outcome.source,
                    now,
                    now,
                    1 if verdict.lossless else 0,
                    now,
                    request_id,
                ),
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
        *,
        user_id: str | None = None,
    ) -> list[dict[str, Any]]:
        sql = (
            "SELECT r.*, d.quality_label, d.size_bytes, d.lossless, d.elapsed_s, "
            "u.display_name AS user_name "
            "FROM requests r LEFT JOIN downloads d ON d.request_id = r.id "
            "LEFT JOIN users u ON u.id = r.user_id"
        )
        clauses: list[str] = []
        params: list[Any] = []
        if status:
            clauses.append("r.status = ?")
            params.append(status)
        if query:
            clauses.append("r.query LIKE ?")
            params.append(f"%{query}%")
        if user_id:
            clauses.append("r.user_id = ?")
            params.append(user_id)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        # Order by last activity, not creation: a review re-download reuses the
        # original (old) request row, so once it finishes it must surface by its
        # finish time rather than sink back to its original created_at position.
        sql += (
            " ORDER BY COALESCE(r.finished_at, r.created_at) DESC, r.created_at DESC "
            "LIMIT ? OFFSET ?"
        )
        params += [limit, offset]
        with self._lock:
            return [dict(row) for row in self._conn.execute(sql, params)]

    def get_request(self, request_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT r.*, u.display_name AS user_name FROM requests r "
                "LEFT JOIN users u ON u.id = r.user_id WHERE r.id=?",
                (request_id,),
            ).fetchone()
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

    _FEED_SELECT = (
        "SELECT r.id, r.query, r.status, r.artist, r.title, r.created_at, r.finished_at, "
        "r.first_playable_at, r.final_flac_at, r.tier_won, r.source_won, r.error, "
        "r.user_id, u.display_name AS user_name, u.avatar_url AS user_avatar, "
        "d.quality_label, d.lossless "
        "FROM requests r LEFT JOIN downloads d ON d.request_id = r.id "
        "LEFT JOIN users u ON u.id = r.user_id "
        "ORDER BY COALESCE(r.finished_at, r.created_at) DESC, r.created_at DESC "
        "LIMIT ? OFFSET ?"
    )

    def list_feed(self, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        """The shared activity feed: every request (any user), newest activity
        first, each carrying who asked for it and both latencies —
        ``ttf_playable_s`` (request -> first playable file) and ``ttf_final_s``
        (request -> final FLAC). A latency is ``None`` until its mark lands, so the
        UI can show "playable in Xs" the instant the rip arrives and fill in "FLAC
        in Ys" when the upgrade completes."""
        with self._lock:
            rows = [dict(r) for r in self._conn.execute(self._FEED_SELECT, (limit, offset))]
        for row in rows:
            row["ttf_playable_s"] = _delta_s(row["created_at"], row["first_playable_at"])
            row["ttf_final_s"] = _delta_s(row["created_at"], row["final_flac_at"])
        return rows

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
    def create_batch(
        self, name: str, origin: str, owner: str, total: int, *, user_id: str | None = None
    ) -> str:
        bid = _new_id()
        with self._lock:
            self._conn.execute(
                "INSERT INTO batches(id, name, origin, owner, total, created_at, user_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (bid, name, origin, owner, total, now_iso(), user_id or SERVICE_USER_ID),
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

    def restore_completed(self, request_id: str) -> bool:
        """Return a request to ``done`` after a failed review **upgrade**.

        A review upgrade that fails on a source/network error (not the fingerprint
        gate) has NOT touched the existing library file — the original download row
        is intact — so the request must not be left ``failed``: that would turn a
        perfectly good (lossy) track into a scary error row. Restore it to ``done``
        so it drops back to a normal lossy item, still upgradeable from Review.
        No-op (returns False) when there's no existing download row to restore."""
        with self._lock:
            has_dl = self._conn.execute(
                "SELECT 1 FROM downloads WHERE request_id=?", (request_id,)
            ).fetchone()
            if has_dl is None:
                return False
            self._conn.execute(
                "UPDATE requests SET status='done', error=NULL, finished_at=? WHERE id=?",
                (now_iso(), request_id),
            )
            self._invalidate_stats()
        return True

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
        now = now_iso()
        # A de-dup reuse is an instant playable file. dedup only reuses lossless
        # matches, so the existing file is the final FLAC too — set both latencies
        # to now (keying final on the copied lossless flag stays correct if a lossy
        # reuse is ever wired in).
        with self._lock:
            self._conn.execute(
                "UPDATE requests SET status='done', tier_won=?, source_won=?, finished_at=?, "
                "first_playable_at=COALESCE(first_playable_at, ?), "
                "final_flac_at=CASE WHEN ? THEN ? ELSE final_flac_at END "
                "WHERE id=?",
                (
                    existing.get("tier_won"),
                    existing.get("source_won"),
                    now,
                    now,
                    1 if existing.get("lossless") else 0,
                    now,
                    request_id,
                ),
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

    # --- review tab (user-assisted retry / re-download) ---
    # Failed requests + `done` rows whose file is lossy (lossless=0), minus the
    # dismissed set. Newest first; each carries the current-file info (lossy) or
    # the failure reason (failed) the console renders.
    _REVIEW_SELECT = (
        "SELECT r.id, r.query, r.requested_via, r.status, r.error, r.created_at, "
        "r.finished_at, r.artist, r.title, r.isrc, r.duration_s, r.tier_won, r.source_won, "
        "r.acoustid_artist, r.acoustid_title, r.acoustid_mbid, r.match_similarity, "
        "d.quality_label, d.size_bytes, d.lossless, d.duration_s AS file_duration_s, "
        "d.path AS file_path, d.codec, d.bitrate "
        "FROM requests r LEFT JOIN downloads d ON d.request_id = r.id "
        "WHERE r.dismissed = 0 AND ("
        "  r.status IN ('failed', 'needs_confirm') OR (r.status = 'done' AND d.lossless = 0)"
        ") ORDER BY r.created_at DESC LIMIT ?"
    )

    def list_review(self, limit: int = 200) -> list[dict[str, Any]]:
        """Items worth a second look: failed requests + lossy `done` downloads.

        Excludes ``dismissed`` rows. Each item is tagged ``kind`` (``failed`` /
        ``lossy`` / ``needs_confirm``); a lossy or needs_confirm item carries its
        current file's quality/path, a failed item its error reason. A
        ``needs_confirm`` item is a review upgrade parked by the fingerprint gate —
        it carries ``match_similarity`` (ADR-0019)."""
        with self._lock:
            rows = [dict(r) for r in self._conn.execute(self._REVIEW_SELECT, (limit,))]
        out: list[dict[str, Any]] = []
        for row in rows:
            status = row["status"]
            kind = status if status in ("failed", "needs_confirm") else "lossy"
            row["kind"] = kind
            row["reason"] = row["error"] if kind == "failed" else None
            row["has_audio"] = bool(row.get("file_path"))
            out.append(row)
        return out

    def review_stats(self) -> dict[str, int]:
        """The review stat-card figures: failed, lossy, needs_confirm (parked by
        the fingerprint gate), pending (their sum), and resolved-via-review
        (targeted re-downloads that completed)."""
        items = self.list_review()
        failed = sum(1 for i in items if i["kind"] == "failed")
        lossy = sum(1 for i in items if i["kind"] == "lossy")
        needs_confirm = sum(1 for i in items if i["kind"] == "needs_confirm")
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM review_candidates rc "
                "JOIN requests r ON r.id = rc.request_id WHERE r.status = 'done'"
            ).fetchone()
            resolved = int(row[0]) if row and row[0] is not None else 0
        return {
            "needs_review": failed + lossy + needs_confirm,
            "failed": failed,
            "lossy": lossy,
            "needs_confirm": needs_confirm,
            "resolved": resolved,
        }

    def dismiss_review(self, request_id: str) -> bool:
        """Remove a request from the review list (``dismissed=1``). Idempotent-ish:
        returns True only when a row actually changed."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE requests SET dismissed=1 WHERE id=? AND dismissed=0", (request_id,)
            )
            self._invalidate_stats()
        return cur.rowcount == 1

    def queue_review_download(
        self,
        request_id: str,
        candidate: Candidate,
        *,
        is_upgrade: bool,
        target_path: str | None,
        force: bool = False,
    ) -> bool:
        """Arm a targeted re-download of ``request_id`` from a chosen candidate.

        Stores the candidate (in ``review_candidates``, keyed by the request) and
        flips the request back to ``queued`` so the serve queue worker claims it and
        downloads exactly this candidate instead of running the cascade. Reusing the
        same request row means a lossy upgrade updates *its* download row in place
        and a fulfilled failure simply becomes ``done`` — both drop out of
        ``list_review`` naturally. ``force`` carries the user's "replace anyway" so
        the worker skips the fingerprint gate for a re-armed ``needs_confirm`` item.
        Returns False when the request no longer exists."""
        with self._lock:
            exists = self._conn.execute(
                "SELECT 1 FROM requests WHERE id=?", (request_id,)
            ).fetchone()
            if exists is None:
                return False
            self._conn.execute(
                "INSERT OR REPLACE INTO review_candidates("
                "request_id, source, tier, ref, title, artist, album, quality_label, "
                "lossless, duration_s, size_bytes, art_url, is_upgrade, target_path, force, "
                "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    request_id,
                    candidate.source,
                    candidate.tier,
                    candidate.ref,
                    candidate.title,
                    candidate.artist,
                    candidate.album,
                    candidate.quality_label,
                    1 if candidate.lossless else 0,
                    candidate.duration_s,
                    candidate.size_bytes,
                    candidate.art_url,
                    1 if is_upgrade else 0,
                    target_path,
                    1 if force else 0,
                    now_iso(),
                ),
            )
            self._conn.execute(
                "UPDATE requests SET status='queued', error=NULL, finished_at=NULL, "
                "dismissed=0 WHERE id=?",
                (request_id,),
            )
            self._invalidate_stats()
        return True

    def force_review_download(self, request_id: str) -> bool:
        """Re-arm the already-stored candidate with ``force`` and requeue it.

        A ``needs_confirm`` item still holds the candidate that failed the gate in
        ``review_candidates``; this is the "replace anyway" path — it flips
        ``force`` on and requeues, so the worker replaces without the gate. No
        client-side candidate is needed (survives a reload). False when nothing is
        armed for this request."""
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM review_candidates WHERE request_id=?", (request_id,)
            ).fetchone()
            if row is None:
                return False
            self._conn.execute(
                "UPDATE review_candidates SET force=1 WHERE request_id=?", (request_id,)
            )
            self._conn.execute(
                "UPDATE requests SET status='queued', error=NULL, finished_at=NULL, "
                "dismissed=0 WHERE id=?",
                (request_id,),
            )
            self._invalidate_stats()
        return True

    def set_match_similarity(self, request_id: str, score: float) -> None:
        """Record the fingerprint similarity of a completed review upgrade."""
        with self._lock:
            self._conn.execute(
                "UPDATE requests SET match_similarity=? WHERE id=?", (score, request_id)
            )
            self._invalidate_stats()

    def mark_needs_confirm(self, request_id: str, score: float) -> None:
        """Park a review upgrade the fingerprint gate rejected (ADR-0019).

        The new file was *not* written; the request keeps its existing (lossy) file
        and moves to ``needs_confirm`` carrying the low ``score`` so the review UI
        can offer "replace anyway" (a ``force`` re-arm) or "keep current"."""
        with self._lock:
            self._conn.execute(
                "UPDATE requests SET status='needs_confirm', match_similarity=?, "
                "finished_at=? WHERE id=?",
                (score, now_iso(), request_id),
            )
            self._invalidate_stats()

    def set_identity_hint(
        self,
        request_id: str,
        *,
        artist: str | None,
        title: str | None,
        mbid: str | None,
        fingerprint: str | None = None,
    ) -> None:
        """Cache the advisory AcoustID hint (+ optional raw fingerprint) on a request."""
        with self._lock:
            self._conn.execute(
                "UPDATE requests SET acoustid_artist=?, acoustid_title=?, acoustid_mbid=?, "
                "ref_fingerprint=COALESCE(?, ref_fingerprint) WHERE id=?",
                (artist, title, mbid, fingerprint, request_id),
            )
            self._invalidate_stats()

    def get_review_candidate(self, request_id: str) -> dict[str, Any] | None:
        """The armed candidate for a claimed request (or ``None`` for an ordinary
        cascade request). The queue worker checks this after ``claim_next``."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM review_candidates WHERE request_id=?", (request_id,)
            ).fetchone()
            return dict(row) if row else None

    # --- Library Sync: what's already downloaded ---
    def library_index(self) -> tuple[set[str], set[str]]:
        """``(isrcs, merge_keys)`` of what's already in the library, so Library Sync
        can tell which playlist and tier tracks are already there. Two sources:
        - every completed request (legacy rows without structured artist/title
          match by ISRC only);
        - every file the dedup scan found on disk, by its tags and by AcoustID's
          identification, so music that never came through crate counts too."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT isrc, artist, title FROM requests WHERE status='done'"
            ).fetchall()
            files = self._conn.execute(
                "SELECT tag_key, aid_artist, aid_title FROM library_files "
                "WHERE missing_since IS NULL"
            ).fetchall()
        isrcs = {str(r["isrc"]).strip().upper() for r in rows if r["isrc"]}
        keys = {merge_key(r["artist"], r["title"]) for r in rows if r["artist"] or r["title"]}
        keys.update(str(f["tag_key"]) for f in files if f["tag_key"])
        keys.update(merge_key(f["aid_artist"], f["aid_title"]) for f in files if f["aid_title"])
        return isrcs, keys

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
