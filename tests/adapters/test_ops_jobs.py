"""/api/jobs REST over a real SqliteJobStore (TestClient)."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from crate.adapters.inbound.ops_api import create_app
from crate.domain.jobs import JobKind
from crate.infrastructure.sqlite.db import connect
from crate.infrastructure.sqlite.job_store import SqliteJobStore
from crate.infrastructure.sqlite.store import RequestStore


def _app(
    tmp_path: Path, manual: frozenset[JobKind] = frozenset({JobKind.LIBRARY_SYNC})
) -> tuple[TestClient, SqliteJobStore]:
    conn = connect(tmp_path / "crate.db")
    jobs = SqliteJobStore(conn)
    client = TestClient(create_app(RequestStore(conn), jobs=jobs, manual_job_kinds=manual))
    return client, jobs


def test_run_now_is_single_flight(tmp_path: Path) -> None:
    client, jobs = _app(tmp_path)
    first = client.post("/api/jobs", json={"kind": "library_sync"}).json()
    assert first["existing"] is False
    again = client.post("/api/jobs", json={"kind": "library_sync"}).json()
    assert again == {"id": first["id"], "existing": True}  # the running/queued one
    job = jobs.get(first["id"])
    assert job is not None and job["triggered_by"] == "manual" and job["user_id"] == "service"


def test_trigger_rejects_unknown_and_non_manual_kinds(tmp_path: Path) -> None:
    client, _ = _app(tmp_path)
    assert client.post("/api/jobs", json={"kind": "rm -rf"}).status_code == 400
    assert client.post("/api/jobs", json={"kind": "dedup_purge"}).status_code == 400  # not allowed


def test_list_detail_and_404(tmp_path: Path) -> None:
    client, _ = _app(tmp_path)
    jid = client.post("/api/jobs", json={"kind": "library_sync"}).json()["id"]
    assert [j["id"] for j in client.get("/api/jobs").json()] == [jid]
    assert client.get("/api/jobs", params={"kind": "dedup_scan"}).json() == []
    assert client.get(f"/api/jobs/{jid}").json()["status"] == "queued"
    assert client.get("/api/jobs/nope").status_code == 404
    assert client.get("/api/jobs", params={"limit": 0}).status_code == 422


def test_events_replay_with_cursor_and_user_scoping(tmp_path: Path) -> None:
    client, jobs = _app(tmp_path)
    jid = client.post("/api/jobs", json={"kind": "library_sync"}).json()["id"]
    jobs.add_event(jid, "info", "global line")
    jobs.add_event(jid, "info", "someone else's tracks", user_id="u2")
    rows = client.get(f"/api/jobs/{jid}/events").json()
    assert [r["message"] for r in rows][-1] == "global line"  # u2's line hidden from service
    after = client.get(f"/api/jobs/{jid}/events", params={"after": rows[0]["id"]}).json()
    assert [r["message"] for r in after] == ["global line"]
    assert client.get("/api/jobs/nope/events").status_code == 404


def test_cancel_queued_then_conflict_then_404(tmp_path: Path) -> None:
    client, _ = _app(tmp_path)
    jid = client.post("/api/jobs", json={"kind": "library_sync"}).json()["id"]
    assert client.post(f"/api/jobs/{jid}/cancel").json() == {"id": jid, "status": "cancelled"}
    assert client.post(f"/api/jobs/{jid}/cancel").status_code == 409  # already finished
    assert client.post("/api/jobs/nope/cancel").status_code == 404


def test_routes_absent_without_a_job_store(tmp_path: Path) -> None:
    client = TestClient(create_app(RequestStore(connect(tmp_path / "crate.db"))))
    assert client.get("/api/jobs").status_code == 404
