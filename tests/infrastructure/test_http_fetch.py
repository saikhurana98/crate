"""http.fetch_bytes — capped in-memory fetch for cover art (no network).

Uses a MockTransport-backed httpx client patched over ``httpx.stream`` so the
streaming path, the status check, and the byte cap are all exercised offline."""

from collections.abc import Iterator

import httpx
import pytest

from crate.infrastructure import http


def _patch_stream(monkeypatch: pytest.MonkeyPatch, handler) -> None:  # type: ignore[no-untyped-def]
    transport = httpx.MockTransport(handler)

    def fake_stream(method: str, url: str, **kwargs: object) -> Iterator[httpx.Response]:
        client = httpx.Client(transport=transport)
        return client.stream(method, url)  # type: ignore[return-value]

    monkeypatch.setattr(http.httpx, "stream", fake_stream)


def test_fetch_bytes_returns_small_body(monkeypatch: pytest.MonkeyPatch) -> None:
    body = b"\xff\xd8jpegbytes\xff\xd9"
    _patch_stream(monkeypatch, lambda req: httpx.Response(200, content=body))
    assert http.fetch_bytes("https://cdn/cover.jpg") == body


def test_fetch_bytes_raises_on_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_stream(monkeypatch, lambda req: httpx.Response(404))
    with pytest.raises(httpx.HTTPStatusError):
        http.fetch_bytes("https://cdn/missing.jpg")


def test_fetch_bytes_aborts_over_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_stream(monkeypatch, lambda req: httpx.Response(200, content=b"x" * 5000))
    with pytest.raises(ValueError, match="cap"):
        http.fetch_bytes("https://cdn/huge.jpg", max_bytes=1000)
