"""/api/navidrome/* and import-time mirroring, over real stores (TestClient)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from crate.adapters.inbound.ops_api import create_app
from crate.application.auth import SpotifyProfile, User
from crate.infrastructure.crypto import SecretBox
from crate.infrastructure.navidrome.client import NavidromeError
from crate.infrastructure.sqlite.db import connect
from crate.infrastructure.sqlite.job_store import SqliteJobStore
from crate.infrastructure.sqlite.navidrome_store import SqliteNavidromeStore
from crate.infrastructure.sqlite.store import RequestStore
from crate.infrastructure.sqlite.user_store import SqliteUserRepository

COOKIE = {"crate_session": "ok"}
TRACK = {"query": "A One", "artist": "A", "title": "One"}


class _Auth:
    def __init__(self, user: User) -> None:
        self.user = user

    def resolve_session(self, raw_token: str) -> User | None:
        return self.user if raw_token == "ok" else None

    def login_url(self, state: str) -> str:
        return ""

    def complete_login(self, code: str) -> tuple[User, str]:
        raise NotImplementedError

    def logout(self, raw_token: str) -> None: ...


class _Library:
    def my_playlists(self, user_id: str) -> list[dict[str, Any]]:
        return []

    def liked_songs(self, user_id: str, max_tracks: int = 2000) -> list[dict[str, Any]]:
        return [TRACK]

    def playlist_tracks(
        self, user_id: str, playlist_id: str, max_tracks: int = 2000
    ) -> list[dict[str, Any]]:
        return [TRACK]


def _verify(user: str, password: str) -> None:
    if password == "bad":
        raise NavidromeError("Wrong username or password", 40)
    if password == "busy":
        raise NavidromeError("server busy", 0)
    if password == "down":
        raise ConnectionError("refused")


class _Env:
    def __init__(self, tmp_path: Path, *, configured: bool = True) -> None:
        conn = connect(tmp_path / "crate.db")
        self.user = SqliteUserRepository(conn).upsert_spotify_user(SpotifyProfile("sp1", "Sai"))
        self.jobs = SqliteJobStore(conn)
        self.nd = SqliteNavidromeStore(conn, SecretBox(b"k" * 32))
        app = create_app(
            RequestStore(conn),
            auth=_Auth(self.user),
            jobs=self.jobs,
            library=_Library(),
            navidrome=self.nd,
            navidrome_verify=_verify if configured else None,
            navidrome_public_url="https://music.example/",
        )
        self.client = TestClient(app)

    def pushes(self) -> list[dict[str, Any]]:
        return self.jobs.list_jobs(kind="navidrome_push")


def test_needs_a_login_and_reports_state(tmp_path: Path) -> None:
    env = _Env(tmp_path)
    assert env.client.get("/api/navidrome").status_code == 401
    got = env.client.get("/api/navidrome", cookies=COOKIE).json()
    assert got == {
        "configured": True,
        "public_url": "https://music.example",
        "account": None,
        "playlists": [],
    }


def test_linking_checks_the_password_first_and_never_returns_it(tmp_path: Path) -> None:
    env = _Env(tmp_path)
    c = env.client
    put = lambda body: c.put("/api/navidrome/account", json=body, cookies=COOKIE)  # noqa: E731
    assert put({"username": "sai"}).status_code == 400
    assert put({"username": "x" * 201, "password": "pw"}).status_code == 400
    bad = put({"username": "sai", "password": "bad"})
    assert bad.status_code == 400 and "rejected" in bad.json()["detail"]
    assert put({"username": "sai", "password": "busy"}).status_code == 502
    assert (
        put({"username": "sai", "password": "down"}).json()["detail"] == "couldn't reach Navidrome"
    )
    assert env.nd.account(env.user.id) is None  # nothing stored on failure
    ok = put({"username": " sai ", "password": "correct horse"})
    assert ok.status_code == 200 and ok.json()["account"]["username"] == "sai"
    assert "correct horse" not in ok.text
    assert "correct horse" not in c.get("/api/navidrome", cookies=COOKIE).text
    assert env.nd.credentials(env.user.id) == ("sai", "correct horse")
    assert [j["user_id"] for j in env.pushes()] == [env.user.id]
    assert c.delete("/api/navidrome/account", cookies=COOKIE).json() == {"removed": True}
    assert env.nd.account(env.user.id) is None


def test_mirror_toggle_and_push_now(tmp_path: Path) -> None:
    env = _Env(tmp_path)
    c = env.client
    body = {"mirror": True, "name": "Mine"}
    assert c.put("/api/navidrome/playlists/p1", json=body, cookies=COOKIE).status_code == 409
    assert c.post("/api/navidrome/push", cookies=COOKIE).status_code == 409
    env.nd.set_account(env.user.id, "sai", "pw")
    r = c.put("/api/navidrome/playlists/p1", json=body, cookies=COOKIE).json()
    assert (r["spotify_id"], r["name"], r["enabled"]) == ("p1", "Mine", True) and r["job"]
    liked = c.put("/api/navidrome/playlists/liked", json={"mirror": True}, cookies=COOKIE).json()
    assert liked["name"] == "Liked Songs"
    off = c.put("/api/navidrome/playlists/p1", json={"mirror": False}, cookies=COOKIE).json()
    assert off["enabled"] is False and off["job"] is None
    assert (
        c.put("/api/navidrome/playlists/zz", json={"mirror": False}, cookies=COOKIE).status_code
        == 404
    )
    assert (
        c.put("/api/navidrome/playlists/p1", json={"mirror": "yes"}, cookies=COOKIE).status_code
        == 400
    )
    assert c.post("/api/navidrome/push", cookies=COOKIE).json()["job"]
    assert len(env.pushes()) == 3


def test_import_mirrors_into_a_linked_account(tmp_path: Path) -> None:
    env = _Env(tmp_path)
    c = env.client
    body = {"source": "playlist", "playlist_id": "p9", "name": "Road trip"}
    assert c.post("/api/library/import", json=body, cookies=COOKIE).json()["navidrome"] is False
    env.nd.set_account(env.user.id, "sai", "pw")
    r = c.post("/api/library/import", json=body, cookies=COOKIE).json()
    assert r["navidrome"] is True and r["total"] == 1
    r = c.post("/api/library/import", json={"source": "liked"}, cookies=COOKIE).json()
    assert r["navidrome"] is True
    links = {link["spotify_id"]: link["name"] for link in env.nd.links(env.user.id)}
    assert links == {"p9": "Road trip", "liked": "Liked Songs"}
    assert [j["params"] for j in env.pushes()] in (
        [{"playlist_id": "liked"}, {"playlist_id": "p9"}],
        [{"playlist_id": "p9"}, {"playlist_id": "liked"}],
    )


def test_not_configured(tmp_path: Path) -> None:
    env = _Env(tmp_path, configured=False)
    c = env.client
    assert c.get("/api/navidrome", cookies=COOKIE).json()["configured"] is False
    r = c.put("/api/navidrome/account", json={"username": "a", "password": "b"}, cookies=COOKIE)
    assert r.status_code == 503
    env.nd.set_account(env.user.id, "sai", "pw")
    body = {"source": "liked"}
    assert c.post("/api/library/import", json=body, cookies=COOKIE).json()["navidrome"] is False
