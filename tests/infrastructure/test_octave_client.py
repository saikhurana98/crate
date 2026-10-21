"""Octave public album catalog normalization."""

from pathlib import Path

import httpx
import pytest

from crate.infrastructure.octave.client import OctaveClient, album_id


def test_album_normalizes_tracks() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://music.octavestreaming.com/api/album/198977692"
        return httpx.Response(
            200,
            json={
                "album": {
                    "title": "Mere Brother Ki Dulhan",
                    "artist": {"name": "SOHAIL SEN"},
                    "tracks": [
                        {
                            "title": "Dhunki",
                            "artist": {"name": "SOHAIL SEN"},
                            "duration": 257,
                        },
                        {"title": "Choomantar", "artist": {"name": "Benny Dayal"}},
                        {"artist": {"name": "missing title"}},
                        "invalid row",
                    ],
                }
            },
        )

    album = OctaveClient(transport=httpx.MockTransport(handler)).album(
        "https://music.octavestreaming.com/album/198977692?ref=share"
    )
    assert album["name"] == "Mere Brother Ki Dulhan"
    assert album["owner"] == "SOHAIL SEN"
    assert album["tracks"] == [
        {
            "query": "SOHAIL SEN Dhunki",
            "artist": "SOHAIL SEN",
            "title": "Dhunki",
            "isrc": None,
            "duration_s": 257,
        },
        {
            "query": "Benny Dayal Choomantar",
            "artist": "Benny Dayal",
            "title": "Choomantar",
            "isrc": None,
            "duration_s": None,
        },
    ]


@pytest.mark.parametrize(
    "ref",
    [
        "http://music.octavestreaming.com/album/1",
        "https://evil.example/album/1",
        "https://music.octavestreaming.com/artist/1",
        "https://music.octavestreaming.com/album/not-a-number",
    ],
)
def test_album_id_rejects_invalid_urls(ref: str) -> None:
    with pytest.raises(ValueError, match="invalid Octave album URL"):
        album_id(ref)


def test_album_rejects_invalid_payload() -> None:
    transport = httpx.MockTransport(lambda _request: httpx.Response(200, json={"album": None}))
    with pytest.raises(ValueError, match="invalid album"):
        OctaveClient(transport=transport).album("https://music.octavestreaming.com/album/1")


def test_search_uses_public_catalog() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["query"] == "Housewife"
        return httpx.Response(200, json={"tracks": [{"id": "1"}]})

    data = OctaveClient(transport=httpx.MockTransport(handler)).search("Housewife")
    assert data == {"tracks": [{"id": "1"}]}


def test_search_retries_transient_empty_catalog() -> None:
    calls = 0
    sleeps: list[float] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"tracks": [] if calls < 3 else [{"id": "1"}]})

    client = OctaveClient(
        transport=httpx.MockTransport(handler), sleep=sleeps.append, search_attempts=3
    )
    assert client.search("Housewife")["tracks"] == [{"id": "1"}]
    assert calls == 3 and sleeps == [1, 2]


def test_verify_account() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer octv_test"
        return httpx.Response(200, json={"ok": True, "userId": "u1"})

    client = OctaveClient(account_key="octv_test", transport=httpx.MockTransport(handler))
    assert client.verify_account() is True
    assert OctaveClient(transport=httpx.MockTransport(handler)).verify_account() is False


def test_fetch_uses_member_token_and_retries_expired_token(tmp_path: Path) -> None:
    tokens = iter(["stale", "fresh"])
    seen_audio: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/playback-token":
            assert request.headers["Authorization"] == "Bearer octv_test"
            return httpx.Response(200, json={"token": next(tokens), "expiresIn": 3600})
        assert request.url.path == "/audio/lossless"
        assert request.url.params["track"] == "4350411742"
        assert request.url.params["dl"] == "1" and request.url.params["raw"] == "1"
        token = request.url.params["k"]
        seen_audio.append(token)
        return httpx.Response(401 if token == "stale" else 200, content=b"fLaC\x00audio")

    dest = tmp_path / "track.part"
    OctaveClient(account_key="octv_test", transport=httpx.MockTransport(handler)).fetch(
        "4350411742", dest
    )
    assert seen_audio == ["stale", "fresh"]
    assert dest.read_bytes() == b"fLaC\x00audio"


def test_fetch_rejects_missing_account_and_bad_track_id(tmp_path: Path) -> None:
    client = OctaveClient(transport=httpx.MockTransport(lambda _request: httpx.Response(500)))
    with pytest.raises(RuntimeError, match="account key"):
        client.fetch("1", tmp_path / "missing")
    with pytest.raises(ValueError, match="track ID"):
        client.fetch("not-an-id", tmp_path / "bad")


def test_playback_authorization_error_is_clear(tmp_path: Path) -> None:
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(403, json={"error": "membership required"})
    )
    client = OctaveClient(account_key="octv_test", transport=transport)
    with pytest.raises(RuntimeError, match="membership required"):
        client.fetch("1", tmp_path / "denied")
