"""SSO sign-in + Spotify linking over real SQLite users/sessions/vault, with fake
identity provider and Spotify."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest

from crate.application.auth import SpotifyProfile
from crate.application.oidc_auth import (
    LinkUnavailableError,
    OidcAuthProvider,
    SpotifyTakenError,
)
from crate.domain.oidc import OidcError, pkce_challenge
from crate.infrastructure.crypto import SecretBox
from crate.infrastructure.sqlite.config_store import ConfigStore
from crate.infrastructure.sqlite.db import connect
from crate.infrastructure.sqlite.user_store import SqliteSessionStore, SqliteUserRepository

ISS = "https://auth.example.com"
NOW = 1_800_000_000.0


def _jwt(claims: dict[str, Any]) -> str:
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    return f"eyJhbGciOiJSUzI1NiJ9.{body}.sig"


class FakeIdp:
    issuer = ISS
    client_id = "crate"

    def __init__(self) -> None:
        self.sub = "sub1"
        self.claims: dict[str, Any] = {"name": "Sai", "email": "s@x.io"}
        self.info: dict[str, Any] | None = {"groups": ["crate-admins"]}
        self.nonces: dict[str, str] = {}
        self.verifiers: dict[str, str] = {}
        self.challenges: dict[str, str] = {}

    def authorize_url(self, state: str, nonce: str, code_challenge: str) -> str:
        self.nonces[state] = nonce
        self.challenges[state] = code_challenge
        return f"{ISS}/authorize?state={state}"

    def exchange(self, code: str, code_verifier: str) -> dict[str, Any]:
        state = code  # tests use the state as the code
        assert pkce_challenge(code_verifier) == self.challenges[state]
        claims = {
            "iss": ISS,
            "aud": "crate",
            "exp": NOW + 300,
            "sub": self.sub,
            "nonce": self.nonces[state],
        }
        return {"id_token": _jwt(claims | self.claims), "access_token": "at"}

    def userinfo(self, access_token: str) -> dict[str, Any] | None:
        return None if self.info is None else {"sub": self.sub} | self.info


class FakeSpotify:
    def __init__(self, sid: str = "sp1") -> None:
        self.sid = sid

    def authorize_url(self, redirect_uri: str, state: str, scope: str) -> str:
        return f"https://accounts.spotify.com/authorize?state={state}&scope={scope}"

    def exchange_code_tokens(self, code: str, redirect_uri: str) -> dict[str, str]:
        return {"access_token": "sat", "refresh_token": f"ref-{code}", "scope": "user-top-read"}

    def me(self, access_token: str) -> dict[str, Any]:
        return {"id": self.sid, "display_name": "spotify sai", "email": None, "avatar": "http://av"}


class World:
    def __init__(self, tmp_path: Path, **kw: Any) -> None:
        self.conn = connect(tmp_path / "c.db")
        self.users = SqliteUserRepository(self.conn)
        self.sessions = SqliteSessionStore(self.conn)
        self.vault = ConfigStore(self.conn, SecretBox(b"k" * 32))
        self.idp = FakeIdp()
        self.spotify = FakeSpotify()
        opts: dict[str, Any] = {
            "label": "Authentik",
            "spotify": self.spotify,
            "spotify_redirect": "https://c/api/auth/callback",
            "spotify_scopes": "user-top-read",
            "clock": lambda: NOW,
        } | kw
        self.p = OidcAuthProvider(self.idp, self.users, self.sessions, self.vault, **opts)

    def sign_in(self) -> tuple[Any, str]:
        state = f"st{len(self.idp.nonces)}"
        self.p.login_url(state)
        return self.p.complete_login(state, state)


def test_sign_in_creates_the_user_and_a_session(tmp_path: Path) -> None:
    w = World(tmp_path, admin_groups=frozenset({"crate-admins"}))
    url = w.p.login_url("abc")
    assert parse_qs(urlparse(url).query)["state"] == ["abc"]
    user, raw = w.p.complete_login("abc", "abc")
    assert (user.oidc_sub, user.display_name, user.email, user.is_admin) == (
        "sub1",
        "Sai",
        "s@x.io",
        True,
    )
    assert w.p.resolve_session(raw) == user and w.p.label == "Authentik"
    w.idp.info = {"groups": []}
    again, _ = w.sign_in()
    assert again.id == user.id and again.is_admin is False  # groups are the source of truth
    w.p.logout(raw)
    assert w.p.resolve_session(raw) is None


def test_state_is_single_use_and_expires(tmp_path: Path) -> None:
    clock = [NOW]
    w = World(tmp_path, clock=lambda: clock[0])
    with pytest.raises(OidcError, match="expired"):
        w.p.complete_login("x", "never-started")
    w.p.login_url("s1")
    w.p.complete_login("s1", "s1")
    with pytest.raises(OidcError, match="already used"):
        w.p.complete_login("s1", "s1")
    w.p.login_url("s2")
    clock[0] += 3600
    with pytest.raises(OidcError, match="expired"):
        w.p.complete_login("s2", "s2")


def test_userinfo_for_another_subject_is_refused(tmp_path: Path) -> None:
    w = World(tmp_path)
    w.idp.info = {"sub": "someone-else"}
    w.p.login_url("s")
    with pytest.raises(OidcError, match="different user"):
        w.p.complete_login("s", "s")
    w.idp.info = None  # no userinfo endpoint: ID-token claims alone
    user, _ = w.sign_in()
    assert user.display_name == "Sai"


def test_pending_sign_ins_are_bounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import crate.application.oidc_auth as mod

    monkeypatch.setattr(mod, "_MAX_PENDING", 2)
    w = World(tmp_path)
    w.p.login_url("a")
    w.p.login_url("b")
    with pytest.raises(OidcError, match="too many"):
        w.p.login_url("c")


def test_old_spotify_sessions_do_not_count_under_sso(tmp_path: Path) -> None:
    w = World(tmp_path)
    legacy = w.users.upsert_spotify_user(SpotifyProfile("sp9", "old"))
    raw = w.sessions.create(legacy.id)
    assert w.p.resolve_session(raw) is None
    assert w.p.resolve_session("nope") is None


def test_link_spotify_stores_tokens_and_applies_spotify_admin(tmp_path: Path) -> None:
    w = World(tmp_path, admin_spotify_ids=frozenset({"sp1"}))
    user, _ = w.sign_in()
    assert user.is_admin is False and w.p.spotify_linkable
    assert "state=L1" in w.p.spotify_link_url("L1")
    linked = w.p.complete_spotify_link("code1", user.id)
    assert (linked.id, linked.spotify_id, linked.is_admin, linked.avatar_url) == (
        user.id,
        "sp1",
        True,
        "http://av",
    )
    assert w.vault.get_secret(f"spotify:{user.id}", "refresh_token") == "ref-code1"
    assert w.vault.get_secret(f"spotify:{user.id}", "scopes") == "user-top-read"
    again, _ = w.sign_in()
    assert again.is_admin is True  # spotify-id admin survives re-sign-in
    w.p.unlink_spotify(user.id)
    got = w.users.get(user.id)
    assert got is not None and (got.spotify_id, got.is_admin) == (None, False)
    assert w.vault.get_secret(f"spotify:{user.id}", "refresh_token") is None


def test_linking_an_older_spotify_account_folds_into_it(tmp_path: Path) -> None:
    w = World(tmp_path)
    legacy = w.users.upsert_spotify_user(SpotifyProfile("sp1", "Spotify Sai"))
    w.vault.put_secret(f"spotify:{legacy.id}", "refresh_token", "old-ref")
    user, raw = w.sign_in()
    w.vault.put_secret(f"spotify:{user.id}", "refresh_token", "stray")
    kept = w.p.complete_spotify_link("c2", user.id)
    assert kept.id == legacy.id and kept.oidc_sub == "sub1" and kept.display_name == "Sai"
    assert w.users.get(user.id) is None
    assert w.p.resolve_session(raw) == kept  # the session followed
    assert w.vault.get_secret(f"spotify:{legacy.id}", "refresh_token") == "ref-c2"
    assert w.vault.get_secret(f"spotify:{user.id}", "refresh_token") is None
    again, _ = w.sign_in()
    assert again.id == legacy.id


def test_a_spotify_account_linked_to_another_sso_user_is_refused(tmp_path: Path) -> None:
    w = World(tmp_path)
    first, _ = w.sign_in()
    w.p.complete_spotify_link("c", first.id)
    w.idp.sub = "sub2"
    second, _ = w.sign_in()
    with pytest.raises(SpotifyTakenError):
        w.p.complete_spotify_link("c", second.id)
    w.spotify.sid = ""
    with pytest.raises(RuntimeError, match="no id"):
        w.p.complete_spotify_link("c", second.id)


def test_linking_unavailable_without_spotify(tmp_path: Path) -> None:
    w = World(tmp_path, spotify=None)
    assert not w.p.spotify_linkable
    with pytest.raises(LinkUnavailableError):
        w.p.spotify_link_url("s")
    with pytest.raises(LinkUnavailableError):
        w.p.complete_spotify_link("c", "u")


def test_no_admin_rule_leaves_admin_alone(tmp_path: Path) -> None:
    w = World(tmp_path)
    user, _ = w.sign_in()
    w.users.set_admin(user.id, True)
    again, _ = w.sign_in()
    assert again.is_admin is True
    linked = w.p.complete_spotify_link("c", user.id)
    assert linked.is_admin is True
