"""Octave public album catalog normalization."""

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
