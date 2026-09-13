"""/api/history/upload + /api/history/status over real stores (TestClient)."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from crate.adapters.inbound.ops_api import create_app
from crate.application.auth import SpotifyProfile, User
from crate.infrastructure.sqlite.db import connect
from crate.infrastructure.sqlite.job_store import SqliteJobStore
from crate.infrastructure.sqlite.listening_store import SqliteListeningStore
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


def _app(
    tmp_path: Path, *, max_bytes: int = 10_000, api_key: str | None = None
) -> tuple[TestClient, SqliteJobStore, SqliteListeningStore, Path, str]:
    conn = connect(tmp_path / "crate.db")
    user = SqliteUserRepository(conn).upsert_spotify_user(SpotifyProfile("sp1", "Sai"))
    jobs, listening = SqliteJobStore(conn), SqliteListeningStore(conn)
    uploads = tmp_path / "uploads"
    app = create_app(
        RequestStore(conn),
        api_key=api_key,
        auth=_Auth(user),
        jobs=jobs,
        listening=listening,
        uploads_dir=uploads,
        history_upload_max_bytes=max_bytes,
    )
    return TestClient(app), jobs, listening, uploads, user.id


def test_upload_streams_to_disk_and_queues_an_import(tmp_path: Path) -> None:
    client, jobs, _, uploads, uid = _app(tmp_path)
    body = json.dumps([{"ts": "2021-01-01T00:00:00Z"}]).encode()
    r = client.post("/api/history/upload", content=body, cookies=COOKIE)
    assert r.status_code == 200 and r.json()["bytes"] == len(body)
    job = jobs.get(r.json()["id"])
    assert job is not None and job["kind"] == "history_import" and job["user_id"] == uid
    stored = uploads / job["params"]["upload"]
    assert stored.read_bytes() == body  # the server-named file holds the raw body
    assert stored.parent == uploads and stored.name.endswith(".upload")


def test_oversized_upload_is_rejected_and_cleaned_up(tmp_path: Path) -> None:
    client, jobs, _, uploads, _ = _app(tmp_path, max_bytes=10)
    r = client.post("/api/history/upload", content=b"x" * 50, cookies=COOKIE)
    assert r.status_code == 413
    assert list(uploads.iterdir()) == [] and jobs.list_jobs() == []


def test_empty_upload_is_rejected(tmp_path: Path) -> None:
    client, _, _, uploads, _ = _app(tmp_path)
    assert client.post("/api/history/upload", content=b"", cookies=COOKIE).status_code == 400
    assert list(uploads.iterdir()) == []


def test_upload_needs_a_real_logged_in_user(tmp_path: Path) -> None:
    client, _, _, _, _ = _app(tmp_path, api_key="k")
    assert client.post("/api/history/upload", content=b"[]").status_code == 401
    # the automation key resolves to the service user, which has no listening history
    r = client.post("/api/history/upload", content=b"[]", headers={"x-api-key": "k"})
    assert r.status_code == 400 and "log in" in r.json()["detail"]


def test_status_reports_the_users_ledger(tmp_path: Path) -> None:
    client, _, listening, _, uid = _app(tmp_path)
    listening.set_export_range(uid, "2019-01-01T00:00:00Z", "2021-06-01T00:00:00Z")
    status = client.get("/api/history/status", cookies=COOKIE).json()
    assert status["plays"] == 0 and status["export"]["start"] == "2019-01-01T00:00:00Z"


def test_routes_absent_without_an_uploads_dir(tmp_path: Path) -> None:
    conn = connect(tmp_path / "crate.db")
    client = TestClient(create_app(RequestStore(conn), jobs=SqliteJobStore(conn)))
    assert client.get("/api/history/status").status_code == 404
