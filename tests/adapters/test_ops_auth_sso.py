"""SSO routes: config, sign-in redirect + callback, Spotify link/unlink, /api/me."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from fastapi.testclient import TestClient

from crate.adapters.inbound.ops_api import create_app
from crate.application.auth import User
from crate.application.oidc_auth import LinkUnavailableError, SpotifyTakenError
from crate.infrastructure.sqlite.db import connect
from crate.infrastructure.sqlite.store import RequestStore

ALICE = User("u-alice", None, "Alice", oidc_sub="sub-a")


class FakeSso:
    label = "Authentik"

    def __init__(self) -> None:
        self.linkable = True
        self.users: dict[str, User] = {}
        self.unlinked: list[str] = []
        self.link_error: Exception | None = None
        self.login_error: Exception | None = None

    @property
    def spotify_linkable(self) -> bool:
        return self.linkable

    def login_url(self, state: str) -> str:
        if self.login_error:
            raise self.login_error
        return f"https://idp/authorize?state={state}"

    def complete_login(self, code: str, state: str) -> tuple[User, str]:
        if code != "good":
            raise ValueError("bad code")
        self.users["tok-a"] = ALICE
        return ALICE, "tok-a"

    def resolve_session(self, raw_token: str) -> User | None:
        return self.users.get(raw_token)

    def logout(self, raw_token: str) -> None:
        self.users.pop(raw_token, None)

    def spotify_link_url(self, state: str) -> str:
        if not self.linkable:
            raise LinkUnavailableError("Spotify linking isn't configured")
        return f"https://accounts.spotify.com/authorize?state={state}"

    def complete_spotify_link(self, code: str, user_id: str) -> User:
        if self.link_error:
            raise self.link_error
        user = User(user_id, "sp1", "Alice", oidc_sub="sub-a")
        self.users["tok-a"] = user
        return user

    def unlink_spotify(self, user_id: str) -> None:
        self.unlinked.append(user_id)


class FakeSpotify:
    def connected(self, user_id: str) -> bool:
        return True

    def missing_scopes(self, user_id: str) -> list[str]:
        return []

    def player(self, user_id: str) -> dict[str, Any]:
        return {}


def _client(
    tmp_path: Path, sso: FakeSso | None = None, spotify: Any = None
) -> tuple[TestClient, FakeSso]:
    sso = sso or FakeSso()
    app = create_app(RequestStore(connect(tmp_path / "c.db")), sso=sso, spotify=spotify)
    return TestClient(app, follow_redirects=False), sso


def _state(resp: Any) -> str:
    return parse_qs(urlparse(resp.headers["location"]).query)["state"][0]


def test_config_and_sign_in(tmp_path: Path) -> None:
    c, sso = _client(tmp_path)
    assert c.get("/api/auth/config").json() == {"mode": "oidc", "label": "Authentik"}
    assert c.get("/api/me").status_code == 401
    r = c.get("/api/auth/login")
    assert r.status_code == 307 and r.headers["location"].startswith("https://idp/authorize")
    bad = c.get("/api/auth/oidc/callback", params={"code": "bad", "state": "s"})
    assert bad.headers["location"] == "/?login=error"
    assert (
        c.get("/api/auth/oidc/callback", params={"error": "access_denied"}).headers["location"]
        == "/?login=error"
    )
    ok = c.get("/api/auth/oidc/callback", params={"code": "good", "state": "s"})
    assert ok.status_code == 303 and "crate_session=tok-a" in ok.headers["set-cookie"]
    me = c.get("/api/me", cookies={"crate_session": "tok-a"}).json()
    assert (me["auth_mode"], me["spotify_linked"], me["spotify_linkable"], me["display_name"]) == (
        "oidc",
        False,
        True,
        "Alice",
    )
    sso.login_error = RuntimeError("discovery down")
    assert c.get("/api/auth/login").headers["location"] == "/?login=error"
    out = c.post("/api/auth/logout", cookies={"crate_session": "tok-a"})
    assert out.headers["location"] == "/?login=out" and "tok-a" not in sso.users


def test_spotify_link_round_trip_is_bound_to_the_session(tmp_path: Path) -> None:
    c, sso = _client(tmp_path, spotify=FakeSpotify())
    sso.users["tok-a"] = ALICE
    assert c.get("/api/auth/spotify/link").headers["location"] == "/"  # not signed in
    r = c.get("/api/auth/spotify/link", cookies={"crate_session": "tok-a"})
    assert r.status_code == 307 and r.headers["location"].startswith("https://accounts.spotify.com")
    state = _state(r)
    # someone else's browser (no session) can't finish it
    stolen = c.get("/api/auth/callback", params={"code": "c", "state": state})
    assert stolen.headers["location"] == "/?spotify=error"
    r = c.get("/api/auth/spotify/link", cookies={"crate_session": "tok-a"})
    done = c.get(
        "/api/auth/callback",
        params={"code": "c", "state": _state(r)},
        cookies={"crate_session": "tok-a"},
    )
    assert done.headers["location"] == "/?spotify=linked"
    replay = c.get(
        "/api/auth/callback",
        params={"code": "c", "state": _state(r)},
        cookies={"crate_session": "tok-a"},
    )
    assert replay.headers["location"] == "/?spotify=error"  # single use
    me = c.get("/api/me", cookies={"crate_session": "tok-a"}).json()
    assert me["spotify_linked"] is True and me["spotify_id"] == "sp1"


def test_link_failures(tmp_path: Path) -> None:
    c, sso = _client(tmp_path)
    sso.users["tok-a"] = ALICE
    cookies = {"crate_session": "tok-a"}
    for err, where in (
        (SpotifyTakenError("x"), "/?spotify=taken"),
        (RuntimeError("x"), "/?spotify=error"),
    ):
        sso.link_error = err
        r = c.get("/api/auth/spotify/link", cookies=cookies)
        got = c.get("/api/auth/callback", params={"code": "c", "state": _state(r)}, cookies=cookies)
        assert got.headers["location"] == where
    r = c.get("/api/auth/spotify/link", cookies=cookies)
    denied = c.get(
        "/api/auth/callback", params={"error": "access_denied", "state": _state(r)}, cookies=cookies
    )
    assert denied.headers["location"] == "/?spotify=error"
    sso.linkable = False
    assert c.get("/api/auth/spotify/link", cookies=cookies).status_code == 503


def test_unlink_and_spotify_mode_routes(tmp_path: Path) -> None:
    c, sso = _client(tmp_path)
    sso.users["tok-a"] = ALICE
    assert c.post("/api/auth/spotify/unlink", cookies={"crate_session": "tok-a"}).json() == {
        "linked": False
    }
    assert sso.unlinked == ["u-alice"]
    # without SSO: Spotify is the sign-in, so there's nothing to link or unlink
    plain = TestClient(create_app(RequestStore(connect(tmp_path / "d.db"))), follow_redirects=False)
    assert plain.get("/api/auth/config").json() == {"mode": "none", "label": "Spotify"}
    assert plain.get("/api/auth/spotify/link").status_code == 404
    assert plain.post("/api/auth/spotify/unlink").status_code == 404
    assert plain.get("/api/auth/oidc/callback").status_code == 503
    assert plain.get("/api/me").json()["auth_mode"] == "none"
