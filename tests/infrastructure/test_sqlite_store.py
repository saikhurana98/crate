"""RequestStore: persistence + read aggregates for the console (ADR-0018).

Uses a real temp-file SQLite DB (WAL) so the migration runner + SQL are exercised
end to end; no mocking of the DB."""

from pathlib import Path

from crate.application.download import DownloadReport
from crate.domain.audio import AudioInfo, QualityVerdict
from crate.domain.download import DownloadOutcome
from crate.infrastructure.sqlite.db import connect, migrate
from crate.infrastructure.sqlite.store import RequestStore


def _store(tmp_path: Path, via: str = "explo") -> RequestStore:
    return RequestStore(connect(tmp_path / "crate.db"), requested_via=via)


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
