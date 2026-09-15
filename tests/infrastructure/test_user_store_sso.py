"""SSO user operations: OIDC upsert, Spotify link/unlink, folding accounts together."""

from __future__ import annotations

from pathlib import Path

import pytest

from crate.application.auth import SpotifyProfile
from crate.domain.navidrome import NavidromeLogin
from crate.domain.oidc import OidcProfile
from crate.infrastructure.crypto import SecretBox
from crate.infrastructure.sqlite.db import connect
from crate.infrastructure.sqlite.navidrome_store import SqliteNavidromeStore
from crate.infrastructure.sqlite.store import RequestStore
from crate.infrastructure.sqlite.user_store import SqliteSessionStore, SqliteUserRepository

ISS = "https://auth.example.com"


def test_oidc_upsert_and_spotify_link(tmp_path: Path) -> None:
    repo = SqliteUserRepository(connect(tmp_path / "c.db"))
    u = repo.upsert_oidc_user(OidcProfile(ISS, "sub1", "Sai", "s@x.io", None))
    assert (u.oidc_sub, u.spotify_id, u.display_name, u.avatar_url) == ("sub1", None, "Sai", None)
    linked = repo.link_spotify(u.id, SpotifyProfile("sp1", "sai", None, "http://spotify-avatar"))
    assert (linked.spotify_id, linked.avatar_url) == ("sp1", "http://spotify-avatar")
    again = repo.upsert_oidc_user(OidcProfile(ISS, "sub1", "Sai K", None, None))
    assert again.id == u.id and again.display_name == "Sai K"
    assert again.avatar_url == "http://spotify-avatar"  # no provider picture: keep Spotify's
    other = repo.upsert_oidc_user(OidcProfile(ISS + "/other", "sub1", "X"))
    assert other.id != u.id  # same sub, different issuer
    repo.unlink_spotify(u.id)
    assert repo.get(u.id).spotify_id is None  # type: ignore[union-attr]


def test_merge_moves_rows_sessions_and_identity(tmp_path: Path) -> None:
    conn = connect(tmp_path / "c.db")
    repo = SqliteUserRepository(conn)
    sessions = SqliteSessionStore(conn)
    nd = SqliteNavidromeStore(conn, SecretBox(b"k" * 32))
    old = repo.upsert_spotify_user(SpotifyProfile("sp1", "Spotify Sai"))  # pre-SSO account
    new = repo.upsert_oidc_user(OidcProfile(ISS, "sub1", "Sai", "s@x.io", username="saik"))
    assert new.sso_username == "saik"
    raw = sessions.create(new.id)
    bid = RequestStore(conn).create_batch("n", "bulk", "n", 1, user_id=new.id)
    nd.set_account(old.id, "old-nd", "pw1")  # one-per-user table: the older row wins
    nd.set_account(new.id, "new-nd", "pw2")
    nd.link(new.id, "p1", "Mine")

    repo.merge_into(new.id, old.id)
    assert repo.get(new.id) is None
    kept = repo.get(old.id)
    assert kept is not None and (kept.oidc_sub, kept.spotify_id, kept.display_name, kept.email) == (
        "sub1",
        "sp1",
        "Sai",
        "s@x.io",
    )
    assert sessions.resolve(raw) == old.id
    assert kept.sso_username == "saik"
    assert conn.execute("SELECT user_id FROM batches WHERE id=?", (bid,)).fetchone()[0] == old.id
    assert nd.credentials(old.id) == NavidromeLogin("old-nd", password="pw1")
    assert [link["spotify_id"] for link in nd.links(old.id)] == ["p1"]
    assert repo.upsert_oidc_user(OidcProfile(ISS, "sub1", "Sai")).id == old.id
    repo.merge_into(old.id, old.id)  # no-op
    with pytest.raises(ValueError, match="no user"):
        repo.merge_into("ghost", old.id)
    assert repo.get(old.id) is not None


def test_merge_of_a_user_without_sso_identity_keeps_the_target_name(tmp_path: Path) -> None:
    repo = SqliteUserRepository(connect(tmp_path / "c.db"))
    a = repo.upsert_spotify_user(SpotifyProfile("sa", "A"))
    b = repo.upsert_spotify_user(SpotifyProfile("sb", "B"))
    repo.merge_into(a.id, b.id)
    got = repo.get(b.id)
    assert got is not None and (got.display_name, got.oidc_sub) == ("B", None)


def test_merge_is_all_or_nothing(tmp_path: Path) -> None:
    conn = connect(tmp_path / "c.db")
    repo = SqliteUserRepository(conn)
    src = repo.upsert_oidc_user(OidcProfile(ISS, "sub1", "Sai"))
    bid = RequestStore(conn).create_batch("n", "bulk", "n", 1, user_id=src.id)
    with pytest.raises(Exception, match="FOREIGN KEY"):
        repo.merge_into(src.id, "no-such-user")  # moving rows to a missing user fails
    assert repo.get(src.id) is not None  # rolled back: nothing moved, nothing deleted
    assert conn.execute("SELECT user_id FROM batches WHERE id=?", (bid,)).fetchone()[0] == src.id
