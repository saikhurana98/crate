"""The arcod client sends the bearer token required by the current API."""

from pathlib import Path

import httpx
import pytest

from crate.infrastructure.arcod.client import ArcodClient


def test_requires_token() -> None:
    with pytest.raises(ValueError, match="bearer token"):
        ArcodClient()


def test_search_and_download_requests_are_authorized(tmp_path: Path) -> None:
    seen: list[tuple[str, str]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.headers["Authorization"]))
        if request.url.path == "/api/get-music":
            return httpx.Response(200, json={"success": True, "data": {}})
        if request.url.path == "/api/v2/downloads":
            return httpx.Response(201, json={"id": "job", "accessToken": "access"})
        if request.url.path == "/api/v2/downloads/job":
            assert request.headers["x-download-token"] == "access"
            return httpx.Response(200, json={"status": "processing"})
        return httpx.Response(200, content=b"audio")

    client = ArcodClient(token="token", transport=httpx.MockTransport(respond))
    try:
        assert client.search("query")["success"] is True
        assert client.create_download({"trackId": "1"})["id"] == "job"
        assert client.poll("job", "access")["status"] == "processing"
        dest = tmp_path / "track.bin"
        client.fetch("https://dl.arcod.xyz/file", dest)
        assert dest.read_bytes() == b"audio"
        assert seen == [("GET", "Bearer token")] * 1 + [("POST", "Bearer token")] + [
            ("GET", "Bearer token"),
            ("GET", "Bearer token"),
        ]
    finally:
        client.close()


def test_preserves_existing_bearer_prefix() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer existing"
        return httpx.Response(200, json={"success": True})

    client = ArcodClient(token="Bearer existing", transport=httpx.MockTransport(respond))
    try:
        client.search("query")
    finally:
        client.close()
