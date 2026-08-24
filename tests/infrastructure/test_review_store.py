"""RequestStore review-tab methods (migration 006): list_review / dismiss /
queue_review_download / review_stats / get_review_candidate.

Real temp-file SQLite so the migration + partial index + SQL run end to end."""

from pathlib import Path

from crate.application.download import DownloadReport
from crate.domain.audio import AudioInfo, QualityVerdict
from crate.domain.candidate import Candidate
from crate.domain.download import DownloadOutcome
from crate.domain.identity import TrackIdentity
from crate.infrastructure.sqlite.db import connect
from crate.infrastructure.sqlite.store import RequestStore


def _store(tmp_path: Path) -> RequestStore:
    return RequestStore(connect(tmp_path / "crate.db"), requested_via="console")


def _report(path: str, source: str, tier: int, *, lossless: bool) -> DownloadReport:
    outcome = DownloadOutcome(Path(path), tier, source, 3.0, "q")
    codec = "flac" if lossless else "opus"
    info = AudioInfo(codec, codec, 44100, 2, 16 if lossless else None, 900_000, 200.0, 5_000_000)
    verdict = QualityVerdict("lossless" if lossless else "lossy opus 160", lossless)
    return DownloadReport("q", outcome, info, verdict, None)


def _lossy_done(store: RequestStore, query: str) -> str:
    rid = store.open_request(query, TrackIdentity(title=query, duration_s=200))
    store.finish_request(rid, _report(f"/nas/{query}.opus", "ytdlp", 3, lossless=False))
    return rid


def _failed(store: RequestStore, query: str) -> str:
    rid = store.open_request(query)
    store.fail_request(rid, "all tiers missed")
    return rid


def _lossless_done(store: RequestStore, query: str) -> str:
    rid = store.open_request(query)
    store.finish_request(rid, _report(f"/nas/{query}.flac", "qobuz", 1, lossless=True))
    return rid


def test_migration_006_adds_dismissed_column_and_review_table(tmp_path: Path) -> None:
    conn = connect(tmp_path / "crate.db")
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(requests)")}
    assert "dismissed" in cols
    tables = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "review_candidates" in tables


def test_list_review_selects_failed_and_lossy_only(tmp_path: Path) -> None:
    store = _store(tmp_path)
    failed = _failed(store, "Failed Track")
    lossy = _lossy_done(store, "Lossy Track")
    _lossless_done(store, "Perfect Track")  # must NOT appear
    store.open_request("In Flight")  # downloading, not reviewable

    items = store.list_review()
    by_id = {i["id"]: i for i in items}
    assert set(by_id) == {failed, lossy}
    assert by_id[failed]["kind"] == "failed" and by_id[failed]["reason"] == "all tiers missed"
    assert by_id[lossy]["kind"] == "lossy" and by_id[lossy]["lossless"] == 0
    assert by_id[lossy]["file_path"] == "/nas/Lossy Track.opus"


def test_dismiss_removes_from_review(tmp_path: Path) -> None:
    store = _store(tmp_path)
    failed = _failed(store, "Dismiss Me")
    assert store.dismiss_review(failed) is True
    assert store.list_review() == []
    assert store.dismiss_review(failed) is False  # already dismissed -> no-op
    assert store.dismiss_review("nope") is False


def test_review_stats_counts(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _failed(store, "F1")
    _failed(store, "F2")
    _lossy_done(store, "L1")
    stats = store.review_stats()
    assert stats == {
        "needs_review": 3,
        "failed": 2,
        "lossy": 1,
        "needs_confirm": 0,
        "resolved": 0,
    }


def _cand() -> Candidate:
    return Candidate(
        "qobuz", 1, "42", "Lossy Track", "Artist", "Album", "FLAC 24/96", True, 200, None
    )


def test_queue_review_download_arms_candidate_and_requeues(tmp_path: Path) -> None:
    store = _store(tmp_path)
    lossy = _lossy_done(store, "Lossy Track")
    ok = store.queue_review_download(
        lossy, _cand(), is_upgrade=True, target_path="/nas/Lossy Track.opus"
    )
    assert ok is True

    # request flipped back to queued so the worker will claim it
    row = store.get_request(lossy)
    assert row is not None and row["status"] == "queued"

    armed = store.get_review_candidate(lossy)
    assert armed is not None
    assert armed["source"] == "qobuz" and armed["ref"] == "42"
    assert armed["is_upgrade"] == 1 and armed["target_path"] == "/nas/Lossy Track.opus"

    claimed = store.claim_next()
    assert claimed is not None and claimed["id"] == lossy


def test_queue_review_download_unknown_request_returns_false(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert store.queue_review_download("nope", _cand(), is_upgrade=False, target_path=None) is False


def test_review_stats_resolved_counts_completed_review_downloads(tmp_path: Path) -> None:
    store = _store(tmp_path)
    lossy = _lossy_done(store, "Lossy Track")
    store.queue_review_download(
        lossy, _cand(), is_upgrade=True, target_path="/nas/Lossy Track.opus"
    )
    store.claim_next()
    # simulate the worker completing the re-download (now a lossless file)
    store.finish_request(lossy, _report("/nas/Lossy Track.opus", "qobuz", 1, lossless=True))
    stats = store.review_stats()
    assert stats["resolved"] == 1
    assert store.list_review() == []  # no longer lossy -> gone from the list


def test_get_review_candidate_none_for_ordinary_request(tmp_path: Path) -> None:
    store = _store(tmp_path)
    rid = store.enqueue("Some Track")
    assert store.get_review_candidate(rid) is None


# --- migration 007: fingerprint guard (ADR-0019) ---


def test_migration_007_adds_fingerprint_columns(tmp_path: Path) -> None:
    conn = connect(tmp_path / "crate.db")
    req = {r["name"] for r in conn.execute("PRAGMA table_info(requests)")}
    assert {
        "acoustid_artist",
        "acoustid_title",
        "acoustid_mbid",
        "ref_fingerprint",
        "match_similarity",
    } <= req
    cand = {r["name"] for r in conn.execute("PRAGMA table_info(review_candidates)")}
    assert "force" in cand


def test_queue_review_download_stores_force(tmp_path: Path) -> None:
    store = _store(tmp_path)
    lossy = _lossy_done(store, "Lossy Track")
    store.queue_review_download(
        lossy, _cand(), is_upgrade=True, target_path="/nas/Lossy Track.opus", force=True
    )
    armed = store.get_review_candidate(lossy)
    assert armed is not None and armed["force"] == 1


def test_mark_needs_confirm_parks_item_with_score(tmp_path: Path) -> None:
    store = _store(tmp_path)
    lossy = _lossy_done(store, "Lossy Track")
    store.queue_review_download(
        lossy, _cand(), is_upgrade=True, target_path="/nas/Lossy Track.opus"
    )
    store.claim_next()
    store.mark_needs_confirm(lossy, 0.42)

    items = {i["id"]: i for i in store.list_review()}
    assert lossy in items
    item = items[lossy]
    assert item["kind"] == "needs_confirm"
    assert item["match_similarity"] == 0.42
    assert item["file_path"] == "/nas/Lossy Track.opus"  # existing file untouched
    assert store.review_stats()["needs_confirm"] == 1


def test_force_review_download_rearms_stored_candidate(tmp_path: Path) -> None:
    store = _store(tmp_path)
    lossy = _lossy_done(store, "Lossy Track")
    store.queue_review_download(
        lossy, _cand(), is_upgrade=True, target_path="/nas/Lossy Track.opus"
    )
    store.claim_next()
    store.mark_needs_confirm(lossy, 0.42)

    assert store.force_review_download(lossy) is True
    armed = store.get_review_candidate(lossy)
    assert armed is not None and armed["force"] == 1
    assert store.get_request(lossy)["status"] == "queued"  # requeued for the worker


def test_force_review_download_false_when_nothing_armed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    rid = _failed(store, "No Candidate")
    assert store.force_review_download(rid) is False


def test_set_match_similarity_records_score(tmp_path: Path) -> None:
    store = _store(tmp_path)
    lossy = _lossy_done(store, "Lossy Track")
    store.set_match_similarity(lossy, 0.98)
    item = next(i for i in store.list_review() if i["id"] == lossy)
    assert item["match_similarity"] == 0.98


def test_set_identity_hint_caches_advisory_fields(tmp_path: Path) -> None:
    store = _store(tmp_path)
    lossy = _lossy_done(store, "Lossy Track")
    store.set_identity_hint(
        lossy, artist="Avicii", title="Lonely Together (acoustic)", mbid="mbid-1"
    )
    item = next(i for i in store.list_review() if i["id"] == lossy)
    assert item["acoustid_artist"] == "Avicii"
    assert item["acoustid_title"] == "Lonely Together (acoustic)"
    assert item["acoustid_mbid"] == "mbid-1"


def test_restore_completed_returns_upgrade_row_to_done(tmp_path: Path) -> None:
    """A failed review-upgrade re-download restores the request to done: the
    existing (lossy) file was never touched, so it must not linger as failed."""
    store = _store(tmp_path)
    rid = _lossy_done(store, "Lossy Track")  # has a download row (the lossy file)
    # simulate the worker having claimed it for an upgrade that then errored
    store.queue_review_download(rid, _cand(), is_upgrade=True, target_path="/nas/Lossy Track.opus")
    store.fail_request(rid, "review download from qobuz failed: 401")
    assert store.get_request(rid)["status"] == "failed"

    assert store.restore_completed(rid) is True
    row = store.get_request(rid)
    assert row["status"] == "done"
    assert row["error"] is None
    assert row["download"]["path"] == "/nas/Lossy Track.opus"  # file preserved
    # still a lossy item in review -> upgradeable again once the source recovers
    assert any(i["id"] == rid and i["kind"] == "lossy" for i in store.list_review())


def test_restore_completed_noop_without_download_row(tmp_path: Path) -> None:
    """A fulfil (failed request, no existing file) can't be 'restored' — returns
    False so the caller fails it cleanly instead."""
    store = _store(tmp_path)
    rid = _failed(store, "Never Landed")
    assert store.restore_completed(rid) is False
    assert store.get_request(rid)["status"] == "failed"
