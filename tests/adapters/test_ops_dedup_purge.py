"""Admin-gated purge / trash routes: who may, what gets queued, validation."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from crate.adapters.inbound.ops_api import create_app
from crate.application.auth import SpotifyProfile, User
from crate.application.library_dedup import GroupRecord, MemberRecord
from crate.domain.library_scan import DiskEntry, ScanPlan
from crate.infrastructure.sqlite.db import connect
from crate.infrastructure.sqlite.dedup_store import SqliteDedupStore
from crate.infrastructure.sqlite.job_store import SqliteJobStore
from crate.infrastructure.sqlite.library_store import SqliteLibraryStore
from crate.infrastructure.sqlite.store import RequestStore
from crate.infrastructure.sqlite.user_store import SqliteUserRepository

COOKIE = {"crate_session": "ok"}


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


class Env:
    def __init__(
        self, tmp_path: Path, *, admin: bool | None = None, api_key: str | None = None
    ) -> None:
        conn = connect(tmp_path / "crate.db")
        lib = SqliteLibraryStore(conn)
        paths = ("a.flac", "b.mp3", "c.mp3", "x.flac", "y.flac", "v1.flac", "v2.flac")
        lib.apply_plan(ScanPlan(new=[DiskEntry(p, 100 + i, 7, None) for i, p in enumerate(paths)]))
        self.ids = {f.path: f.id for f in lib.indexed()}
        self.dedup = SqliteDedupStore(conn)
        m = self._member
        self.dedup.replace_groups(
            [
                GroupRecord(
                    "g1",
                    "acoustid",
                    0.97,
                    False,
                    self.ids["a.flac"],
                    203,
                    [m("a.flac", 0), m("b.mp3", 1), m("c.mp3", 2)],
                ),
                GroupRecord(
                    "gx",
                    "exact",
                    1.0,
                    False,
                    self.ids["x.flac"],
                    104,
                    [m("x.flac", 0), m("y.flac", 1)],
                ),
                GroupRecord(
                    "gv",
                    "fp",
                    0.93,
                    True,
                    self.ids["v1.flac"],
                    0,
                    [m("v1.flac", 0), m("v2.flac", 1)],
                ),
            ]
        )
        self.jobs = SqliteJobStore(conn)
        users = SqliteUserRepository(conn)
        auth = None
        if admin is not None:
            user = users.upsert_spotify_user(SpotifyProfile("sp1", "Sai"))
            if admin:
                users.set_admin(user.id, True)
                user = User(user.id, user.spotify_id, user.display_name, is_admin=True)
            auth = _Auth(user)
        self.client = TestClient(
            create_app(
                RequestStore(conn), dedup=self.dedup, jobs=self.jobs, auth=auth, api_key=api_key
            )
        )

    def _member(self, path: str, rank: int) -> MemberRecord:
        return MemberRecord(
            self.ids[path], rank, None if rank == 0 else 0.99, [] if rank == 0 else ["x"], {}
        )

    def post(self, url: str, body: object = None, **kw: object) -> object:
        return self.client.post(url, json=body if body is not None else {}, cookies=COOKIE, **kw)  # type: ignore[arg-type]


def test_non_admins_get_403_but_can_still_read(tmp_path: Path) -> None:
    env = Env(tmp_path, admin=False)
    for url, body in (
        ("/api/dedup/groups/g1/purge", {}),
        ("/api/dedup/resolve-exact", {}),
        ("/api/dedup/trash/restore", {"ids": ["t"]}),
        ("/api/dedup/trash/empty", {"confirm": "EMPTY"}),
    ):
        assert env.post(url, body).status_code == 403, url  # type: ignore[attr-defined]
    assert env.client.get("/api/dedup/trash", cookies=COOKIE).json() == {
        "items": [],
        "count": 0,
        "bytes": 0,
    }
    assert env.client.get("/api/dedup/overview", cookies=COOKIE).json()["mutating"] is False


def test_admin_purge_queues_what_the_user_saw(tmp_path: Path) -> None:
    env = Env(tmp_path, admin=True)
    r = env.post("/api/dedup/groups/g1/purge")
    assert r.status_code == 200 and r.json()["files"] == 2  # type: ignore[attr-defined]
    job = env.jobs.get(r.json()["job"])  # type: ignore[attr-defined]
    assert job is not None and job["kind"] == "dedup_purge"
    items = job["params"]["items"]
    assert {i["file_id"] for i in items} == {env.ids["b.mp3"], env.ids["c.mp3"]}
    assert all(i["keeper_id"] == env.ids["a.flac"] and i["mtime_ns"] == 7 for i in items)
    assert env.client.get("/api/dedup/overview", cookies=COOKIE).json()["mutating"] is True
    # keep a different copy, trash one explicit other
    body = {"keep": env.ids["b.mp3"], "trash": [env.ids["c.mp3"]]}
    assert env.post("/api/dedup/groups/g1/purge", body).json()["files"] == 1  # type: ignore[attr-defined]


def test_purge_validation(tmp_path: Path) -> None:
    env = Env(tmp_path, admin=True)
    a, b = env.ids["a.flac"], env.ids["b.mp3"]
    assert env.post("/api/dedup/groups/nope/purge").status_code == 404  # type: ignore[attr-defined]
    assert env.post("/api/dedup/groups/g1/purge", {"keep": env.ids["x.flac"]}).status_code == 400  # type: ignore[attr-defined]
    assert env.post("/api/dedup/groups/g1/purge", {"keep": a, "trash": [a]}).status_code == 400  # type: ignore[attr-defined]
    assert env.post("/api/dedup/groups/g1/purge", {"trash": [env.ids["x.flac"]]}).status_code == 400  # type: ignore[attr-defined]
    assert env.post("/api/dedup/groups/g1/purge", {"trash": "b"}).status_code == 400  # type: ignore[attr-defined]
    assert env.post("/api/dedup/groups/gv/purge").status_code == 400  # type: ignore[attr-defined]  # variants: explicit only
    ok = env.post("/api/dedup/groups/gv/purge", {"trash": [env.ids["v2.flac"]]})
    assert ok.status_code == 200 and b  # type: ignore[attr-defined]


def test_resolve_exact_restore_and_empty(tmp_path: Path) -> None:
    env = Env(tmp_path, admin=True)
    ov = env.client.get("/api/dedup/overview", cookies=COOKIE).json()
    assert (ov["exact_groups"], ov["exact_files"], ov["exact_bytes"]) == (1, 1, 104)
    r = env.post("/api/dedup/resolve-exact").json()  # type: ignore[attr-defined]
    assert r["files"] == 1 and r["bytes"] == 104
    assert env.post("/api/dedup/trash/restore", {"ids": []}).status_code == 400  # type: ignore[attr-defined]
    restore = env.post("/api/dedup/trash/restore", {"ids": ["t1"]}).json()  # type: ignore[attr-defined]
    assert env.jobs.get(restore["job"])["params"] == {"ids": ["t1"]}  # type: ignore[index]
    assert env.post("/api/dedup/trash/empty", {}).status_code == 400  # type: ignore[attr-defined]
    empty = env.post("/api/dedup/trash/empty", {"confirm": "EMPTY"}).json()  # type: ignore[attr-defined]
    assert empty["existing"] is False and env.jobs.get(empty["job"])["params"] == {"all": True}  # type: ignore[index]


def test_resolve_exact_404_when_nothing_is_identical(tmp_path: Path) -> None:
    env = Env(tmp_path, admin=True)
    env.dedup.replace_groups([])
    assert env.post("/api/dedup/resolve-exact").status_code == 404  # type: ignore[attr-defined]


def test_api_key_and_single_user_mode_count_as_admin(tmp_path: Path) -> None:
    keyed = Env(tmp_path / "k", api_key="secret")
    r = keyed.client.post("/api/dedup/groups/g1/purge", json={}, headers={"X-API-Key": "secret"})
    assert r.status_code == 200
    solo = Env(tmp_path / "s")
    assert solo.client.post("/api/dedup/groups/g1/purge", json={}).status_code == 200
