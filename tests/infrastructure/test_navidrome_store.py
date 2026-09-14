"""Per-user Navidrome accounts, mirror links, and the song id cache."""

from __future__ import annotations

from pathlib import Path

from crate.application.auth import SpotifyProfile
from crate.infrastructure.crypto import SecretBox
from crate.infrastructure.sqlite.db import connect
from crate.infrastructure.sqlite.navidrome_store import SqliteNavidromeStore
from crate.infrastructure.sqlite.user_store import SqliteUserRepository


def _store(tmp_path: Path) -> tuple[SqliteNavidromeStore, str, object]:
    conn = connect(tmp_path / "crate.db")
    uid = SqliteUserRepository(conn).upsert_spotify_user(SpotifyProfile("sp1", "Sai")).id
    return SqliteNavidromeStore(conn, SecretBox(b"k" * 32)), uid, conn


def test_account_is_encrypted_and_never_read_back_in_plain(tmp_path: Path) -> None:
    store, uid, conn = _store(tmp_path)
    assert store.account(uid) is None and store.credentials(uid) is None
    store.set_account(uid, "sai", "s3cret-pw")
    raw = conn.execute("SELECT password_ct FROM navidrome_accounts").fetchone()[0]  # type: ignore[attr-defined]
    assert b"s3cret-pw" not in bytes(raw)
    acct = store.account(uid)
    assert acct is not None and acct["username"] == "sai" and "password" not in str(acct)
    assert store.credentials(uid) == ("sai", "s3cret-pw")
    store.set_account(uid, "sai2", "new")
    assert store.credentials(uid) == ("sai2", "new") and store.account_users() == [uid]


def test_links_and_pushes(tmp_path: Path) -> None:
    store, uid, _ = _store(tmp_path)
    store.set_account(uid, "sai", "pw")
    store.link(uid, "p1", "Mine")
    store.link(uid, "liked", "Liked Songs")
    assert [link["spotify_id"] for link in store.links(uid)] == ["p1", "liked"]
    store.record_push(uid, "p1", navidrome_id="nd1", matched=2, in_library=3, total=5)
    store.record_push(uid, "p1", error="boom")  # keeps the last good numbers
    row = store.links(uid)[0]
    assert (row["navidrome_id"], row["matched"], row["total"], row["error"]) == (
        "nd1",
        2,
        5,
        "boom",
    )
    assert store.set_enabled(uid, "p1", False) is True
    assert [link["spotify_id"] for link in store.links(uid, enabled_only=True)] == ["liked"]
    assert store.set_enabled(uid, "nope", False) is False
    store.link(uid, "p1", "Mine (renamed)")  # re-mirroring keeps the Navidrome id
    row = next(link for link in store.links(uid) if link["spotify_id"] == "p1")
    assert (row["enabled"], row["navidrome_id"], row["name"]) == (True, "nd1", "Mine (renamed)")
    assert store.delete_account(uid) is True and store.links(uid) == []
    assert store.delete_account(uid) is False and store.account_users() == []


def test_song_cache_freshness(tmp_path: Path) -> None:
    store, _, conn = _store(tmp_path)
    for i in range(600):  # more than one IN-chunk
        store.put_song(f"tok:k{i}", f"s{i}")
    got = store.song_ids([f"tok:k{i}" for i in range(600)] + ["tok:none"], fresh_after="2000-01-01")
    assert len(got) == 600 and got["tok:k599"] == "s599"
    conn.execute("UPDATE navidrome_songs SET checked_at='2001-01-01' WHERE merge_key='tok:k1'")  # type: ignore[attr-defined]
    assert "tok:k1" not in store.song_ids(["tok:k1", "tok:k2"], fresh_after="2020-01-01")
    store.put_song("tok:k1", "s1b")
    assert store.song_ids(["tok:k1"], fresh_after="2020-01-01") == {"tok:k1": "s1b"}
