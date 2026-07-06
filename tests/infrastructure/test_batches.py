"""Bulk batches in the RequestStore: grouping, claim-skipping, pause/cancel/retry."""

from pathlib import Path

from crate.infrastructure.sqlite.db import connect
from crate.infrastructure.sqlite.store import RequestStore


def _store(tmp_path: Path) -> RequestStore:
    return RequestStore(connect(tmp_path / "b.db"), requested_via="console")


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
