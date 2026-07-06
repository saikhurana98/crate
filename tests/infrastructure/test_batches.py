"""Bulk batches in the RequestStore: grouping, claim-skipping, pause/cancel/retry."""

from pathlib import Path

from crate.infrastructure.sqlite.db import connect
from crate.infrastructure.sqlite.store import RequestStore


def _store(tmp_path: Path) -> RequestStore:
    return RequestStore(connect(tmp_path / "b.db"), requested_via="console")


def _rep():
    from pathlib import Path as _P

    from crate.application.download import DownloadReport
    from crate.domain.audio import AudioInfo, QualityVerdict
    from crate.domain.download import DownloadOutcome

    o = DownloadOutcome(_P("/nas/x.flac"), 1, "qobuz", 4.1, "q")
    i = AudioInfo("flac", "flac", 96000, 2, 24, 1800000, 248.0, 94000000)
    return DownloadReport("q", o, i, QualityVerdict("lossless 24/96", True), None)


def test_batch_groups_requests_with_counts(tmp_path: Path) -> None:
    s = _store(tmp_path)
    bid = s.create_batch("My Playlist", "spotify", "sai", 3)
    for q in ["a x", "b y", "c z"]:
        s.enqueue(q, batch_id=bid)
    [b] = s.list_batches()
    assert b["name"] == "My Playlist" and b["origin"] == "spotify" and b["owner"] == "sai"
    assert b["total"] == 3 and b["paused"] is False
    assert b["counts"] == {"done": 0, "failed": 0, "downloading": 0, "queued": 3}
    assert len(s.get_batch(bid)["tracks"]) == 3


def test_claim_skips_paused_batch(tmp_path: Path) -> None:
    s = _store(tmp_path)
    bid = s.create_batch("PL", "paste", "", 2)
    s.enqueue("a", batch_id=bid)
    s.enqueue("b", batch_id=bid)
    assert s.claim_next() is not None  # one goes downloading
    s.set_batch_paused(bid, True)
    assert s.claim_next() is None  # remaining queued row is blocked
    s.set_batch_paused(bid, False)
    assert s.claim_next() is not None  # unblocked


def test_cancel_drops_queued_only(tmp_path: Path) -> None:
    s = _store(tmp_path)
    bid = s.create_batch("PL", "paste", "", 2)
    s.enqueue("a", batch_id=bid)
    s.enqueue("b", batch_id=bid)
    s.claim_next()  # 'a' -> downloading
    assert s.cancel_batch(bid) is True
    [b] = s.list_batches()
    # the downloading one survives; the queued one is cancelled
    assert b["counts"]["queued"] == 0 and b["counts"]["downloading"] == 1
    assert b["cancelled_flag"] is True
    assert s.claim_next() is None  # cancelled batch is not claimable


def test_requeue_retries_failed(tmp_path: Path) -> None:
    s = _store(tmp_path)
    rid = s.enqueue("x")
    s.claim_next()
    s.fail_request(rid, "no source")
    assert s.requeue(rid) is True
    assert s.claim_next() == {"id": rid, "query": "x"}
    # a done/queued request is not retryable
    assert s.requeue(rid) is False


def test_missing_batch_ops_return_false(tmp_path: Path) -> None:
    s = _store(tmp_path)
    assert s.set_batch_paused("nope", True) is False
    assert s.cancel_batch("nope") is False
    assert s.get_batch("nope") is None


def test_concurrent_reads_dont_corrupt_cursors(tmp_path: Path) -> None:
    """The console shares one connection across threads (SSE + threadpool routes).
    Hammer the read methods concurrently — without serialising, cursors interleave
    and stats()/list_* raise (IndexError / 'database is locked'). Must stay clean."""
    import threading

    s = _store(tmp_path)
    bid = s.create_batch("PL", "spotify", "sai", 20)
    for i in range(20):
        rid = s.enqueue(f"track {i}", batch_id=bid)
        if i % 3 == 0:
            s.claim_next()
            s.finish_request(rid, _rep())

    errors: list[Exception] = []

    def hammer() -> None:
        try:
            for _ in range(40):
                s.stats()
                s.list_requests(limit=50)
                s.list_batches()
                s.get_batch(bid)
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=hammer) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
