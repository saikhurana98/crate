"""Bulk resolution dispatch — playlist links vs pasted lists (no network)."""

import pytest

from crate.application.bulk import resolve_bulk


def _spotify(_ref: str) -> dict:
    return {"name": "Focus", "owner": "sai", "tracks": ["A x", "B y"]}


def _youtube(_url: str) -> dict:
    return {"name": "YT mix", "owner": "", "tracks": ["C z"]}


def test_spotify_playlist_link() -> None:
    b = resolve_bulk(
        "https://open.spotify.com/playlist/37i9?si=abc",
        spotify_playlist=_spotify,
        youtube_playlist=_youtube,
    )
    assert b.origin == "spotify" and b.name == "Focus" and b.owner == "sai"
    assert b.tracks == ["A x", "B y"]


def test_spotify_uri() -> None:
    b = resolve_bulk("spotify:playlist:37i9", spotify_playlist=_spotify)
    assert b.origin == "spotify" and b.tracks == ["A x", "B y"]


def test_youtube_playlist_link() -> None:
    b = resolve_bulk("https://www.youtube.com/playlist?list=PL123", youtube_playlist=_youtube)
    assert b.origin == "youtube" and b.name == "YT mix" and b.tracks == ["C z"]


def test_pasted_list() -> None:
    b = resolve_bulk("Daft Punk Get Lucky\n  Burial Archangel \n\nBonobo Kerala")
    assert b.origin == "paste"
    assert b.tracks == ["Daft Punk Get Lucky", "Burial Archangel", "Bonobo Kerala"]
    assert "3 tracks" in b.name


def test_single_line_uses_it_as_name() -> None:
    b = resolve_bulk("Aphex Twin Rhubarb")
    assert (
        b.origin == "paste"
        and b.name == "Aphex Twin Rhubarb"
        and b.tracks == ["Aphex Twin Rhubarb"]
    )


def test_spotify_link_without_resolver_is_lookup_error() -> None:
    with pytest.raises(LookupError):
        resolve_bulk("https://open.spotify.com/playlist/x")


def test_youtube_link_without_resolver_is_lookup_error() -> None:
    with pytest.raises(LookupError):
        resolve_bulk("https://youtube.com/playlist?list=x")


def test_empty_input_is_value_error() -> None:
    with pytest.raises(ValueError):
        resolve_bulk("   \n  ")


def test_playlist_with_no_tracks_is_value_error() -> None:
    with pytest.raises(ValueError):
        resolve_bulk(
            "https://open.spotify.com/playlist/x",
            spotify_playlist=lambda _r: {"name": "Empty", "tracks": []},
        )
