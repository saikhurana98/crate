from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from crate.infrastructure.antra.client import AntraClient


def test_download_track_logs_in_submits_polls_and_fetches(tmp_path: Path) -> None:
    calls: list[tuple[str, str]] = []
    polls = iter([{"status": "running", "done": 0}, {"status": "complete", "done": 1}])

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.url.path == "/api/auth/login":
            return httpx.Response(200, json={"ok": True}, headers={"set-cookie": "session=x"})
        if request.url.path == "/api/jobs":
            body = __import__("json").loads(request.content)
            assert body["format"] == "lossless-16"
            assert body["tracks"][0]["isrc"] == "US123"
            assert body["client_packaging"] is True
            return httpx.Response(200, json={"job_id": "job-1"})
        if request.url.path == "/api/jobs/job-1/status":
            return httpx.Response(200, json=next(polls))
        if request.url.path == "/api/jobs/job-1/tracks/0/download":
            return httpx.Response(200, content=b"fLaC\x00audio")
        raise AssertionError(request.url.path)

    client = AntraClient(
        "user",
        "pass",
        "https://antra.test",
        transport=httpx.MockTransport(handler),
        sleep=lambda _: None,
    )
    dest = tmp_path / "track.part"
    client.download_track({"title": "Song", "artists": ["Artist"], "isrc": "US123"}, dest)

    assert dest.read_bytes().startswith(b"fLaC")
    assert calls == [
        ("POST", "/api/auth/login"),
        ("POST", "/api/jobs"),
        ("GET", "/api/jobs/job-1/status"),
        ("GET", "/api/jobs/job-1/status"),
        ("GET", "/api/jobs/job-1/tracks/0/download"),
    ]


def test_login_and_quota_errors_are_actionable() -> None:
    def bad_login(_: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"detail": "Invalid credentials"})

    client = AntraClient("u", "bad", transport=httpx.MockTransport(bad_login))
    with pytest.raises(RuntimeError, match="Invalid credentials"):
        client.submit_track({"title": "x"})

    def quota(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/auth/login":
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(
            429,
            json={"detail": "Daily limit reached"},
            headers={"X-Antra-Error-Code": "DAILY_LIMIT"},
        )

    client = AntraClient("u", "p", transport=httpx.MockTransport(quota))
    with pytest.raises(RuntimeError, match=r"DAILY_LIMIT.*Daily limit reached"):
        client.submit_track({"title": "x"})


def test_failed_job_surfaces_server_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/auth/login":
            return httpx.Response(200, json={"ok": True})
        if request.url.path == "/api/jobs":
            return httpx.Response(200, json={"job_id": "j"})
        return httpx.Response(200, json={"status": "failed", "error": "No source matched"})

    client = AntraClient("u", "p", transport=httpx.MockTransport(handler))
    with pytest.raises(RuntimeError, match="No source matched"):
        client.download_track({"title": "x"}, Path("unused"))
