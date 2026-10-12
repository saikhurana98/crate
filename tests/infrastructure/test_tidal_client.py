"""Rhythmax TIDAL catalog, manifest, DASH assembly, and URL safety."""

from pathlib import Path

import httpx
import pytest

from crate.infrastructure.tidal.client import TidalClient

MPD = b"""<?xml version="1.0"?>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011">
  <Period><AdaptationSet><Representation>
    <SegmentTemplate startNumber="1"
      initialization="https://sp-ad-fa.audio.tidal.com/init.mp4"
      media="https://sp-ad-fa.audio.tidal.com/segment-$Number%03d$.m4s">
      <SegmentTimeline><S t="0" d="10" r="1"/></SegmentTimeline>
    </SegmentTemplate>
  </Representation></AdaptationSet></Period>
</MPD>"""


def test_search_manifest_and_segmented_fetch(tmp_path: Path) -> None:
    requests: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:  # noqa: PLR0911
        requests.append(f"{request.method} {request.url}")
        if request.url.host == "auth.odskyler.workers.dev":
            return httpx.Response(200, json={"access_token": "catalog", "expires_in": 3600})
        if request.url.path == "/token":
            return httpx.Response(200, json={"token": "playback"})
        if request.url.path == "/v1/search/tracks":
            assert request.headers["authorization"] == "Bearer catalog"
            assert request.url.params["countryCode"] == "US"
            return httpx.Response(200, json={"items": [{"id": 7}]})
        if request.url.path == "/manifests":
            assert request.headers["authorization"] == "Bearer playback"
            return httpx.Response(
                200,
                json={
                    "data": {
                        "attributes": {
                            "trackPresentation": "FULL",
                            "uri": "https://im-fa.manifest.tidal.com/track.mpd",
                        }
                    }
                },
            )
        if request.url.host == "im-fa.manifest.tidal.com":
            return httpx.Response(200, content=MPD)
        if request.url.path == "/init.mp4":
            return httpx.Response(206, content=b"init")
        if request.url.path == "/segment-001.m4s":
            return httpx.Response(206, content=b"one")
        if request.url.path == "/segment-002.m4s":
            return httpx.Response(206, content=b"two")
        return httpx.Response(404)

    def remux(source: Path, dest: Path) -> None:
        assert source.read_bytes() == b"initonetwo"
        dest.write_bytes(b"fLaCdone")

    client = TidalClient(transport=httpx.MockTransport(respond), remux=remux)
    try:
        assert client.search("Ashke")["items"][0]["id"] == 7
        destination = tmp_path / "song.flac"
        client.fetch("562641091", destination)
        assert destination.read_bytes() == b"fLaCdone"
        assert sum("/token" in request for request in requests) == 2
        assert not list(tmp_path.glob("*.mp4"))
    finally:
        client.close()


def test_fetch_rejects_untrusted_media_host_and_cleans_up(tmp_path: Path) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/token":
            return httpx.Response(200, json={"token": "playback"})
        if request.url.path == "/manifests":
            return httpx.Response(
                200,
                json={"data": {"attributes": {"uri": "https://evil.example/track.mpd"}}},
            )
        return httpx.Response(500)

    client = TidalClient(transport=httpx.MockTransport(respond))
    try:
        with pytest.raises(ValueError, match="untrusted"):
            client.fetch("1", tmp_path / "song.flac")
        assert not list(tmp_path.iterdir())
    finally:
        client.close()


def test_manifest_rejects_non_numeric_id() -> None:
    client = TidalClient(
        transport=httpx.MockTransport(lambda request: pytest.fail("unexpected request"))
    )
    try:
        with pytest.raises(ValueError, match="invalid TIDAL track ID"):
            client.manifest("../bad")
    finally:
        client.close()
