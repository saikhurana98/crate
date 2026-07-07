"""RequestStore: persistence + read aggregates for the console (ADR-0018).

Uses a real temp-file SQLite DB (WAL) so the migration runner + SQL are exercised
end to end; no mocking of the DB."""

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from crate.application.download import DownloadReport
from crate.domain.audio import AudioInfo, QualityVerdict
from crate.domain.download import DownloadOutcome
from crate.infrastructure.sqlite.db import connect, migrate
from crate.infrastructure.sqlite.store import RequestStore


def _store(tmp_path: Path, via: str = "explo") -> RequestStore:
    return RequestStore(connect(tmp_path / "crate.db"), requested_via=via)


class _CountingConn:
    """Wraps a real connection, counting SELECT executes so a test can assert a
    cached stats() call ran no fresh queries. Delegates everything else."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self.selects = 0

    def execute(self, sql: str, *args: object) -> sqlite3.Cursor:
        if sql.lstrip().upper().startswith("SELECT"):
            self.selects += 1
        return self._conn.execute(sql, *args)

    def __getattr__(self, name: str) -> object:
        return getattr(self._conn, name)


def _report(source: str = "qobuz", tier: int = 1, lossless: bool = True) -> DownloadReport:
    outcome = DownloadOutcome(Path(f"/nas/{source}.flac"), tier, source, 4.2, "Daft Punk Get Lucky")
    info = AudioInfo("flac", "flac", 96000, 2, 24, 1_800_000, 248.0, 94_000_000)
    verdict = QualityVerdict("lossless 24-bit / 96 kHz flac", lossless)
    return DownloadReport("Daft Punk Get Lucky", outcome, info, verdict, None)


def test_migration_creates_schema_and_is_idempotent(tmp_path: Path) -> None:
    conn = connect(tmp_path / "crate.db")
    tables = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"requests", "attempts", "downloads", "schema_migrations"} <= tables
    # re-running finds nothing to apply
    assert migrate(conn) == []


def test_open_then_finish_records_winner_and_download(tmp_path: Path) -> None:
    store = _store(tmp_path)
    rid = store.open_request("Daft Punk Get Lucky")

    inflight = store.list_requests(status="downloading")
    assert len(inflight) == 1 and inflight[0]["id"] == rid
    assert inflight[0]["requested_via"] == "explo"

    store.record_attempt(rid, "monochrome", 1, False, "Turnstile JWT required")
    store.record_attempt(rid, "qobuz", 1, True, "lossless 24-bit / 96 kHz flac")
    store.finish_request(rid, _report())

    got = store.get_request(rid)
    assert got is not None
    assert got["status"] == "done"
    assert got["tier_won"] == 1 and got["source_won"] == "qobuz"
    assert got["finished_at"] is not None
    assert [(a["source"], a["ok"]) for a in got["attempts"]] == [("monochrome", 0), ("qobuz", 1)]
    dl = got["download"]
    assert dl["size_bytes"] == 94_000_000 and dl["bit_depth"] == 24
    assert dl["lossless"] == 1 and dl["elapsed_s"] == 4.2


def test_fail_request_sets_status_and_error(tmp_path: Path) -> None:
    store = _store(tmp_path)
    rid = store.open_request("Nonexistent Track")
    store.fail_request(rid, "no source produced a file")
    got = store.get_request(rid)
    assert got is not None
    assert got["status"] == "failed" and got["error"] == "no source produced a file"
    assert got["download"] is None


def test_get_request_unknown_id_returns_none(tmp_path: Path) -> None:
    assert _store(tmp_path).get_request("does-not-exist") is None


def test_list_filters_by_status_and_query(tmp_path: Path) -> None:
    store = _store(tmp_path)
    a = store.open_request("Fleetwood Mac Dreams")
    store.finish_request(a, _report(source="qobuz"))
    b = store.open_request("Daft Punk Get Lucky")
    store.fail_request(b, "boom")

    assert len(store.list_requests()) == 2
    assert [r["id"] for r in store.list_requests(status="done")] == [a]
    assert [r["id"] for r in store.list_requests(status="failed")] == [b]
    hits = store.list_requests(query="fleetwood")
    assert len(hits) == 1 and hits[0]["id"] == a
    # done rows carry the joined quality label
    assert store.list_requests(status="done")[0]["quality_label"].startswith("lossless")


def test_list_pagination(tmp_path: Path) -> None:
    store = _store(tmp_path)
    for i in range(5):
        store.open_request(f"track {i}")
    page = store.list_requests(limit=2, offset=0)
    assert len(page) == 2
    assert len(store.list_requests(limit=2, offset=4)) == 1


def test_stats_aggregates(tmp_path: Path) -> None:
    store = _store(tmp_path)
    r1 = store.open_request("t1")
    store.record_attempt(r1, "monochrome", 1, False, "err")
    store.record_attempt(r1, "qobuz", 1, True, "ok")
    store.finish_request(r1, _report(source="qobuz", tier=1, lossless=True))
    r2 = store.open_request("t2")
    store.record_attempt(r2, "qobuz", 1, True, "ok")
    store.finish_request(r2, _report(source="qobuz", tier=1, lossless=True))
    r3 = store.open_request("t3")
    store.record_attempt(r3, "ytdlp", 3, False, "boom")
    store.fail_request(r3, "boom")
    store.open_request("t4")  # still downloading

    s = store.stats()
    assert s["total"] == 4
    assert s["done"] == 2 and s["failed"] == 1 and s["in_flight"] == 1
    assert s["success_rate"] == 2 / 3
    assert s["lossless"] == 2 and s["lossless_rate"] == 1.0
    assert s["wins_by_source"] == {"qobuz": 2}
    assert s["wins_by_tier"] == {"1": 2}
    assert s["attempts_by_source"]["qobuz"] == {"ok": 2, "total": 2}
    assert s["attempts_by_source"]["monochrome"] == {"ok": 0, "total": 1}
    assert s["total_bytes"] == 188_000_000


def test_stats_empty_db_has_safe_zeroes(tmp_path: Path) -> None:
    s = _store(tmp_path).stats()
    assert s["total"] == 0
    assert s["success_rate"] == 0.0 and s["lossless_rate"] == 0.0
    assert s["total_bytes"] == 0 and s["avg_elapsed_s"] == 0
    assert s["wins_by_source"] == {}


def test_enqueue_then_claim_runs_fifo_and_drains(tmp_path: Path) -> None:
    store = _store(tmp_path, via="console")
    a = store.enqueue("first")
    store.enqueue("second")

    queued = store.list_requests(status="queued")
    assert len(queued) == 2 and queued[-1]["query"] == "first"
    assert store.stats()["queued"] == 2

    claimed = store.claim_next()
    assert claimed == {"id": a, "query": "first"}  # oldest first
    got = store.get_request(a)
    assert got is not None and got["status"] == "downloading" and got["requested_via"] == "console"
    assert store.stats()["queued"] == 1

    store.claim_next()
    assert store.claim_next() is None  # queue drained


def test_reap_orphans_requeues_stuck_downloads(tmp_path: Path) -> None:
    # Simulate a process that died mid-flight: rows left 'downloading'. A fresh
    # serve must requeue them so the workers re-claim (else they hang forever).
    store = _store(tmp_path, via="console")
    a = store.enqueue("first")
    b = store.enqueue("second")
    store.claim_next()  # a -> downloading
    store.claim_next()  # b -> downloading
    done = store.open_request("already-done")
    store.finish_request(done, _report())
    assert store.stats()["in_flight"] == 2 and store.stats()["queued"] == 0

    assert store.reap_orphans() == 2  # both downloading rows requeued

    assert store.stats()["queued"] == 2 and store.stats()["in_flight"] == 0
    # they are claimable again, oldest first, and finished rows are untouched
    assert store.claim_next() == {"id": a, "query": "first"}
    assert store.claim_next() == {"id": b, "query": "second"}
    assert store.get_request(done) is not None and store.get_request(done)["status"] == "done"


def test_reap_orphans_noop_when_nothing_in_flight(tmp_path: Path) -> None:
    store = _store(tmp_path, via="console")
    store.enqueue("queued-not-touched")
    assert store.reap_orphans() == 0
    assert store.stats()["queued"] == 1


def test_finish_with_incomplete_report_is_noop(tmp_path: Path) -> None:
    store = _store(tmp_path)
    rid = store.open_request("t")
    store.finish_request(rid, DownloadReport("t", None, None, None, "partial"))
    got = store.get_request(rid)
    assert got is not None and got["status"] == "downloading" and got["download"] is None


# --- stats() cache (P0-B) ---


def test_stats_cache_hits_within_ttl_without_requerying(tmp_path: Path) -> None:
    """The SSE stream polls stats() every 2 s per client; a second call inside the
    TTL must return the same frame without touching the DB again."""
    counting = _CountingConn(connect(tmp_path / "crate.db"))
    clock = [1000.0]
    store = RequestStore(counting, requested_via="console", now=lambda: clock[0])  # type: ignore[arg-type]
    store.open_request("t")

    first = store.stats()
    counting.selects = 0  # ignore the queries the first (uncached) call ran
    clock[0] = 1001.5  # still within the 2.0 s TTL
    second = store.stats()

    assert second is first  # exact cached object, not just equal
    assert counting.selects == 0  # no fresh reads


def test_stats_cache_expires_after_ttl(tmp_path: Path) -> None:
    counting = _CountingConn(connect(tmp_path / "crate.db"))
    clock = [1000.0]
    store = RequestStore(counting, requested_via="console", now=lambda: clock[0])  # type: ignore[arg-type]
    store.open_request("t")

    store.stats()
    counting.selects = 0
    clock[0] = 1002.01  # past the 2.0 s TTL
    store.stats()

    assert counting.selects > 0  # recomputed


def test_every_write_busts_the_stats_cache(tmp_path: Path) -> None:
    """Each write method must invalidate the cache so a reader never serves a
    frame that predates its own write."""
    store = _store(tmp_path, via="console")

    def total() -> int:
        return int(store.stats()["total"])

    assert total() == 0  # primes the cache
    store.open_request("a")
    assert total() == 1  # busted by open_request

    rid = store.enqueue("b")
    assert total() == 2  # busted by enqueue

    store.claim_next()  # 'b' queued -> downloading (joins 'a' already downloading)
    assert store.stats()["in_flight"] == 2  # busted by claim_next

    store.record_attempt(rid, "qobuz", 1, True, "ok")
    store.stats()  # busted by record_attempt (attempts_by_source refreshed)
    store.finish_request(rid, _report())
    assert store.stats()["done"] == 1  # busted by finish_request

    fid = store.open_request("c")
    store.fail_request(fid, "boom")
    assert store.stats()["failed"] == 1  # busted by fail_request

    store.requeue(fid)
    assert store.stats()["queued"] == 1  # busted by requeue

    store.reap_orphans()
    store.stats()  # busted by reap_orphans

    bid = store.create_batch("PL", "paste", "", 1)
    store.stats()  # busted by create_batch
    store.set_batch_paused(bid, True)
    store.stats()  # busted by set_batch_paused
    store.cancel_batch(bid)
    store.stats()  # busted by cancel_batch

    # 'b' finished above, so it is now a completed library entry we can reuse.
    existing = store.find_completed("b")
    assert existing is not None
    done_before = store.stats()["done"]
    dup = store.enqueue("b")
    store.complete_as_duplicate(dup, existing)
    assert store.stats()["done"] == done_before + 1  # busted by complete_as_duplicate


# --- attempts retention (P0-B) ---


def test_prune_attempts_deletes_only_old_rows(tmp_path: Path) -> None:
    store = _store(tmp_path)
    rid = store.open_request("t")
    store.record_attempt(rid, "qobuz", 1, True, "recent")  # created_at = now
    # hand-insert an ancient attempt row (created_at 200 days ago)
    old = (datetime.now(UTC) - timedelta(days=200)).isoformat()
    conn = connect(tmp_path / "crate.db")
    conn.execute(
        "INSERT INTO attempts(request_id, source, tier, ok, detail, created_at) "
        "VALUES (?, 'ytdlp', 3, 0, 'ancient', ?)",
        (rid, old),
    )

    assert store.prune_attempts(older_than_days=90) == 1  # only the ancient row
    got = store.get_request(rid)
    assert got is not None
    assert [a["detail"] for a in got["attempts"]] == ["recent"]


def test_prune_attempts_noop_when_all_recent(tmp_path: Path) -> None:
    store = _store(tmp_path)
    rid = store.open_request("t")
    store.record_attempt(rid, "qobuz", 1, True, "ok")
    assert store.prune_attempts() == 0


# --- index usage (P0-B) ---


def test_find_completed_uses_query_status_index(tmp_path: Path) -> None:
    """find_completed runs once per download; its requests(query,status) filter
    must hit idx_requests_query_status, not full-scan."""
    conn = connect(tmp_path / "crate.db")
    plan = conn.execute(
        "EXPLAIN QUERY PLAN "
        "SELECT r.tier_won, r.source_won, d.* FROM requests r "
        "JOIN downloads d ON d.request_id = r.id "
        "WHERE r.query = ? AND r.status = 'done' "
        "ORDER BY r.finished_at DESC LIMIT 1",
        ("some track",),
    ).fetchall()
    details = " | ".join(row["detail"] for row in plan)
    assert "USING INDEX idx_requests_query_status" in details


def test_migration_004_creates_perf_indexes(tmp_path: Path) -> None:
    conn = connect(tmp_path / "crate.db")
    indexes = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert {
        "idx_requests_query_status",
        "idx_requests_status_created",
        "idx_attempts_source_ok",
        "idx_batches_flags",
    } <= indexes
