"""Phase 2: request/batch attribution (user_id) + owner-round-robin fair
scheduling in claim_next."""

from __future__ import annotations

from pathlib import Path

from crate.application.auth import SpotifyProfile
from crate.infrastructure.sqlite.db import connect
from crate.infrastructure.sqlite.store import RequestStore
from crate.infrastructure.sqlite.user_store import SqliteUserRepository


def _store_with_users(tmp_path: Path) -> tuple[RequestStore, str, str]:
    conn = connect(tmp_path / "crate.db")
    repo = SqliteUserRepository(conn)
    a = repo.upsert_spotify_user(SpotifyProfile("spa", "Alice")).id
    b = repo.upsert_spotify_user(SpotifyProfile("spb", "Bob")).id
    return RequestStore(conn), a, b


def test_enqueue_defaults_to_service_and_accepts_user(tmp_path: Path) -> None:
    store, alice, _ = _store_with_users(tmp_path)
    r_default = store.enqueue("q1")
    r_alice = store.enqueue("q2", user_id=alice)
    rows = {r["id"]: r for r in store.list_requests()}
    assert rows[r_default]["user_id"] == "service"
    assert rows[r_alice]["user_id"] == alice
    assert rows[r_alice]["user_name"] == "Alice"  # joined display name


def test_open_request_and_batch_attribution(tmp_path: Path) -> None:
    store, alice, _ = _store_with_users(tmp_path)
    rid = store.open_request("direct", user_id=alice)
    assert store.get_request(rid)["user_id"] == alice
    bid = store.create_batch("Mix", "spotify", "Alice", 0, user_id=alice)
    row = next(b for b in store.list_batches() if b["id"] == bid)
    assert row["user_id"] == alice


def test_list_requests_user_filter(tmp_path: Path) -> None:
    store, alice, bob = _store_with_users(tmp_path)
    store.enqueue("a", user_id=alice)
    store.enqueue("b", user_id=bob)
    mine = store.list_requests(user_id=alice)
    assert len(mine) == 1 and mine[0]["query"] == "a"


def test_claim_next_round_robin_prevents_starvation(tmp_path: Path) -> None:
    """Alice floods 20 requests, then Bob adds 1. Bob's single request must be
    served within the first two claims — not stuck behind Alice's backlog."""
    store, alice, bob = _store_with_users(tmp_path)
    for i in range(20):
        store.enqueue(f"alice-{i}", user_id=alice)
    store.enqueue("bob-only", user_id=bob)

    first = store.claim_next()
    second = store.claim_next()
    claimed = {first["query"], second["query"]}  # type: ignore[index]
    assert "bob-only" in claimed  # Bob served early despite Alice's 20-deep queue


def test_claim_next_fifo_within_a_user(tmp_path: Path) -> None:
    store, alice, _ = _store_with_users(tmp_path)
    store.enqueue("first", user_id=alice)
    store.enqueue("second", user_id=alice)
    assert store.claim_next()["query"] == "first"  # type: ignore[index]
    assert store.claim_next()["query"] == "second"  # type: ignore[index]
