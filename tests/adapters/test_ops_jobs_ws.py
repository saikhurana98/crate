"""/api/ws/jobs: backlog + live tail, resume cursor, per-user scoping, auth
(4401), cross-origin guard (4403), idle pings. Real SqliteJobStore; the socket's
poll interval is shrunk so the tail is observed in milliseconds."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from crate.adapters.inbound.ops_api import create_app
from crate.adapters.inbound.ops_jobs import job_frame, origin_allowed
from crate.application.auth import User
from crate.domain.jobs import JobKind
from crate.infrastructure.sqlite.db import connect
from crate.infrastructure.sqlite.job_store import SqliteJobStore
from crate.infrastructure.sqlite.store import RequestStore


def _app(tmp_path: Path, **kw: Any) -> tuple[TestClient, SqliteJobStore]:
    conn = connect(tmp_path / "crate.db")
    jobs = SqliteJobStore(conn)
    app = create_app(RequestStore(conn), jobs=jobs, ws_poll_interval=0.02, **kw)
    return TestClient(app), jobs


def test_backlog_then_live_tail(tmp_path: Path) -> None:
    client, jobs = _app(tmp_path)
    jid, _ = jobs.enqueue(JobKind.LIBRARY_SYNC, triggered_by="schedule")
    with client.websocket_connect("/api/ws/jobs") as ws:
        first = ws.receive_json()
        assert first["type"] == "events" and first["events"][-1]["code"] == "queued"
        assert [j["id"] for j in first["active"]] == [jid]  # "sync running" badge data
        jobs.add_event(jid, "info", "checked 3 playlists")
        live = ws.receive_json()
        assert [e["message"] for e in live["events"]] == ["checked 3 playlists"]
        assert live["cursor"] > first["cursor"]


def test_since_resumes_after_the_cursor(tmp_path: Path) -> None:
    client, jobs = _app(tmp_path)
    jid, _ = jobs.enqueue(JobKind.LIBRARY_SYNC, triggered_by="schedule")
    seen = jobs.add_event(jid, "info", "first")
    jobs.add_event(jid, "info", "second")
    with client.websocket_connect(f"/api/ws/jobs?since={seen}") as ws:
        frame = ws.receive_json()
        assert [e["message"] for e in frame["events"]] == ["second"]  # nothing repeated


def test_resume_with_nothing_new_keeps_the_cursor(tmp_path: Path) -> None:
    client, jobs = _app(tmp_path)
    jid, _ = jobs.enqueue(JobKind.LIBRARY_SYNC, triggered_by="schedule")
    last = jobs.add_event(jid, "info", "only")
    with client.websocket_connect(f"/api/ws/jobs?since={last}") as ws:
        frame = ws.receive_json()
        assert frame["events"] == [] and frame["cursor"] == last


def test_empty_log_starts_at_the_newest_id(tmp_path: Path) -> None:
    client, _ = _app(tmp_path)
    with client.websocket_connect("/api/ws/jobs") as ws:
        frame = ws.receive_json()
        assert frame["events"] == [] and frame["cursor"] == 0 and frame["active"] == []


def test_user_scoped_lines_only_reach_their_user(tmp_path: Path) -> None:
    client, jobs = _app(tmp_path)
    jid, _ = jobs.enqueue(JobKind.LIBRARY_SYNC, triggered_by="schedule")
    jobs.add_event(jid, "info", "friend's tracks", user_id="u2")
    with client.websocket_connect("/api/ws/jobs") as ws:  # viewer = service
        backlog = ws.receive_json()
        assert "friend's tracks" not in [e["message"] for e in backlog["events"]]
        jobs.add_event(jid, "info", "more of friend's", user_id="u2")
        jobs.add_event(jid, "info", "global progress")
        live = ws.receive_json()
        assert [e["message"] for e in live["events"]] == ["global progress"]


def test_unauthenticated_socket_closes_4401(tmp_path: Path) -> None:
    client, _ = _app(tmp_path, api_key="secret")
    with (
        pytest.raises(WebSocketDisconnect) as exc,
        client.websocket_connect("/api/ws/jobs") as ws,
    ):
        ws.receive_json()
    assert exc.value.code == 4401


def test_api_key_header_authenticates_the_socket(tmp_path: Path) -> None:
    client, _ = _app(tmp_path, api_key="secret")
    with client.websocket_connect("/api/ws/jobs", headers={"x-api-key": "secret"}) as ws:
        assert ws.receive_json()["type"] == "events"


class _FakeAuth:
    RAW = "sess-raw"

    def resolve_session(self, raw_token: str) -> User | None:
        return (
            User(id="service", spotify_id="s", display_name="Sai")
            if raw_token == self.RAW
            else None
        )

    def login_url(self, state: str) -> str:
        return ""

    def complete_login(self, code: str) -> tuple[User, str]:
        raise NotImplementedError

    def logout(self, raw_token: str) -> None: ...


def test_session_cookie_authenticates_the_socket(tmp_path: Path) -> None:
    client, _ = _app(tmp_path, auth=_FakeAuth())
    with client.websocket_connect(
        "/api/ws/jobs", headers={"cookie": "crate_session=sess-raw"}
    ) as ws:
        assert ws.receive_json()["type"] == "events"
    with (
        pytest.raises(WebSocketDisconnect) as exc,
        client.websocket_connect("/api/ws/jobs", headers={"cookie": "crate_session=bad"}) as ws,
    ):
        ws.receive_json()
    assert exc.value.code == 4401


def test_cross_origin_upgrade_closes_4403(tmp_path: Path) -> None:
    client, _ = _app(tmp_path)
    with (
        pytest.raises(WebSocketDisconnect) as exc,
        client.websocket_connect("/api/ws/jobs", headers={"origin": "https://evil.example"}) as ws,
    ):
        ws.receive_json()
    assert exc.value.code == 4403


def test_same_origin_upgrade_is_allowed(tmp_path: Path) -> None:
    client, _ = _app(tmp_path)
    with client.websocket_connect("/api/ws/jobs", headers={"origin": "http://testserver"}) as ws:
        assert ws.receive_json()["type"] == "events"


def test_idle_socket_gets_keepalive_pings(tmp_path: Path) -> None:
    client, _ = _app(tmp_path, ws_ping_interval=0.05)
    with client.websocket_connect("/api/ws/jobs") as ws:
        ws.receive_json()  # backlog
        assert ws.receive_json()["type"] == "ping"


def test_socket_route_absent_without_a_job_store(tmp_path: Path) -> None:
    client = TestClient(create_app(RequestStore(connect(tmp_path / "crate.db"))))
    with pytest.raises(WebSocketDisconnect), client.websocket_connect("/api/ws/jobs") as ws:
        ws.receive_json()


def test_origin_allowed_and_frame_helpers() -> None:
    assert origin_allowed({"host": "flac.example"}) is True  # no Origin: non-browser
    assert origin_allowed({"origin": "https://flac.example", "host": "flac.example"}) is True
    assert origin_allowed({"origin": "https://FLAC.example", "host": "flac.example"}) is True
    assert origin_allowed({"origin": "https://evil.example", "host": "flac.example"}) is False
    assert job_frame([], 7, []) == {"type": "events", "events": [], "cursor": 7, "active": []}
