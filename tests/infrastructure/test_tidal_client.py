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
            assert request.url.params["quality"] == "HI_RES_LOSSLESS"
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

    client = TidalClient(transport=httpx.MockTransport(respond), manifest_attempts=1)
    try:
        with pytest.raises(ValueError, match="untrusted"):
            client.fetch("1", tmp_path / "song.flac")
        assert not list(tmp_path.iterdir())
    finally:
        client.close()


def test_manifest_falls_back_to_lossless_when_hires_qualities_fail() -> None:
    qualities: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/token":
            return httpx.Response(200, json={"token": "playback"})
        quality = request.url.params["quality"]
        qualities.append(quality)
        if quality != "LOSSLESS":
            return httpx.Response(500, json={"error": "Client cannot access this content"})
        return httpx.Response(200, json={"data": {"attributes": {"uri": "ok"}}})

    client = TidalClient(transport=httpx.MockTransport(respond), manifest_attempts=1)
    try:
        assert client.manifest("236908111")["data"]["attributes"]["uri"] == "ok"
        assert qualities == ["HI_RES_LOSSLESS", "HI_RES", "LOSSLESS"]
    finally:
        client.close()


def test_mpd_selects_highest_quality_flac_representation() -> None:
    mpd = b"""<MPD xmlns="urn:mpeg:dash:schema:mpd:2011"><Period><AdaptationSet>
      <Representation id="AACLC" codecs="mp4a.40.2" audioSamplingRate="44100">
        <SegmentTemplate initialization="https://sp.audio.tidal.com/aac-init"
          media="https://sp.audio.tidal.com/aac-$Number$.m4s">
          <SegmentTimeline><S d="1"/></SegmentTimeline>
        </SegmentTemplate>
      </Representation>
      <Representation id="FLAC" codecs="flac" bandwidth="1000" audioSamplingRate="44100">
        <SegmentTemplate initialization="https://sp.audio.tidal.com/cd-init"
          media="https://sp.audio.tidal.com/cd-$Number$.m4s">
          <SegmentTimeline><S d="1"/></SegmentTimeline>
        </SegmentTemplate>
      </Representation>
      <Representation id="FLAC_HIRES" codecs="flac" bandwidth="2000" audioSamplingRate="96000">
        <SegmentTemplate initialization="https://sp.audio.tidal.com/hires-init"
          media="https://sp.audio.tidal.com/hires-$Number$.m4s">
          <SegmentTimeline><S d="1"/></SegmentTimeline>
        </SegmentTemplate>
      </Representation>
    </AdaptationSet></Period></MPD>"""

    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=mpd)

    client = TidalClient(transport=httpx.MockTransport(respond))
    try:
        assert client._segment_urls("https://im-fa.manifest.tidal.com/track.mpd") == [
            "https://sp.audio.tidal.com/hires-init",
            "https://sp.audio.tidal.com/hires-1.m4s",
        ]
    finally:
        client.close()


def test_manifest_retries_transient_wrapped_upstream_error() -> None:
    calls = 0
    sleeps: list[float] = []

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        if request.url.path == "/token":
            return httpx.Response(200, json={"token": "playback"})
        calls += 1
        if calls == 1:
            return httpx.Response(500, json={"error": "TIDAL request failed (429)"})
        return httpx.Response(200, json={"data": {"attributes": {"uri": "ok"}}})

    client = TidalClient(transport=httpx.MockTransport(respond), sleep=sleeps.append)
    try:
        assert client.manifest("236908111")["data"]["attributes"]["uri"] == "ok"
        assert calls == 2
        assert sleeps == [1]
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
