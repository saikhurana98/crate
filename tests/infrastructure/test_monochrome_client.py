"""The direct Monochrome client uses the site's public search and FLAC routes."""

from pathlib import Path

import httpx
import pytest

from crate.infrastructure.monochrome.client import MonochromeClient


def test_search_and_stream_use_current_routes_without_head(tmp_path: Path) -> None:
    methods: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        if request.url.path == "/search/tracks":
            assert request.url.params["q"] == "Zara Sa"
            assert request.url.params["limit"] == "20"
            return httpx.Response(200, json={"tracks": [{"trackId": "123"}]})
        assert request.url.path == "/track/123"
        return httpx.Response(200, content=b"fLaC" + b"\0" * 64)

    client = MonochromeClient(
        "https://tracks.monochrome.st", transport=httpx.MockTransport(respond)
    )
    try:
        assert client.search("Zara Sa")["tracks"][0]["trackId"] == "123"
        dest = tmp_path / "track.flac"
        client.fetch("123", dest)
        assert dest.read_bytes().startswith(b"fLaC")
        assert methods == ["GET", "GET"]
    finally:
        client.close()


def test_fetch_rejects_non_numeric_id_before_network(tmp_path: Path) -> None:
    client = MonochromeClient(
        transport=httpx.MockTransport(lambda request: pytest.fail("unexpected request"))
    )
    try:
        with pytest.raises(ValueError, match="invalid Monochrome track ID"):
            client.fetch("../evil", tmp_path / "track.flac")
    finally:
        client.close()


def test_fetch_resumes_after_truncated_response(tmp_path: Path) -> None:
    calls = 0

    class BrokenStream(httpx.SyncByteStream):
        def __iter__(self):  # type: ignore[no-untyped-def]
            yield b"fLaC" + b"a" * 65_532
            raise httpx.RemoteProtocolError("peer closed early")

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            assert "range" not in request.headers
            return httpx.Response(200, stream=BrokenStream())
        assert request.headers["range"] == "bytes=65536-"
        return httpx.Response(
            206,
            headers={"Content-Range": "bytes 65536-65539/65540"},
            content=b"done",
        )

    client = MonochromeClient(transport=httpx.MockTransport(respond))
    try:
        dest = tmp_path / "track.flac"
        client.fetch("123", dest)
        assert dest.read_bytes() == b"fLaC" + b"a" * 65_532 + b"done"
        assert calls == 2
    finally:
        client.close()


def test_search_retries_429_using_retry_after() -> None:
    calls = 0
    sleeps: list[float] = []

    def respond(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls < 3:
            return httpx.Response(429, headers={"Retry-After": "2"})
        return httpx.Response(200, json={"tracks": []})

    client = MonochromeClient(transport=httpx.MockTransport(respond), sleep=sleeps.append)
    try:
        assert client.search("Song") == {"tracks": []}
        assert calls == 3
        assert sleeps == [2.0, 2.0]
    finally:
        client.close()


def test_fetch_retries_headerless_429_with_bounded_backoff(tmp_path: Path) -> None:
    calls = 0
    sleeps: list[float] = []

    def respond(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls < 3:
            return httpx.Response(429, json={"error": "rate limited"})
        return httpx.Response(200, content=b"fLaCdone")

    client = MonochromeClient(transport=httpx.MockTransport(respond), sleep=sleeps.append)
    try:
        dest = tmp_path / "track.flac"
        client.fetch("123", dest)
        assert dest.read_bytes() == b"fLaCdone"
        assert calls == 3
        assert sleeps == [1.0, 2.0]
    finally:
        client.close()


def test_fetch_rate_limit_retries_are_bounded(tmp_path: Path) -> None:
    sleeps: list[float] = []

    client = MonochromeClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(429)),
        sleep=sleeps.append,
    )
    try:
        with pytest.raises(RuntimeError, match="rate limited after bounded retries"):
            client.fetch("123", tmp_path / "track.flac")
        assert sleeps == [1.0, 2.0]
    finally:
        client.close()


@pytest.mark.parametrize("operation", ["search", "fetch"])
def test_excessive_retry_after_does_not_retry_early(operation: str, tmp_path: Path) -> None:
    calls = 0

    def respond(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(429, headers={"Retry-After": "120"})

    client = MonochromeClient(
        transport=httpx.MockTransport(respond),
        sleep=lambda _: pytest.fail("should fall through without waiting"),
    )
    try:
        with pytest.raises(RuntimeError, match="rate limited"):
            if operation == "search":
                client.search("Song")
            else:
                client.fetch("123", tmp_path / "track.flac")
        assert calls == 1
    finally:
        client.close()
