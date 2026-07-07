"""Bulk resolution dispatch — playlist links vs pasted lists (no network).

``ResolvedBatch.tracks`` are :class:`TrackIdentity` now: Spotify playlist dicts
carry structured artist/title/isrc/duration; YouTube + pasted lines are parsed
best-effort from their free text via ``from_query``."""

import pytest

from crate.application.bulk import resolve_bulk
from crate.domain.identity import TrackIdentity


def _spotify(_ref: str) -> dict:
    return {
        "name": "Focus",
        "owner": "sai",
        "tracks": [
            {
                "query": "A x",
                "artist": "A",
                "title": "x",
                "isrc": "US1234567890",
                "duration_s": 210,
            },
            {"query": "B y", "artist": "B", "title": "y", "isrc": None, "duration_s": None},
        ],
    }


def _youtube(_url: str) -> dict:
    return {"name": "YT mix", "owner": "", "tracks": ["C z"]}


def test_spotify_playlist_link() -> None:
    b = resolve_bulk(
        "https://open.spotify.com/playlist/37i9?si=abc",
        spotify_playlist=_spotify,
        youtube_playlist=_youtube,
    )
    assert b.origin == "spotify" and b.name == "Focus" and b.owner == "sai"
    # structured identity threaded through, including ISRC + duration
    assert b.tracks[0] == TrackIdentity("A", "x", "US1234567890", 210)
    assert b.tracks[1] == TrackIdentity("B", "y", None, None)
    assert [t.display() for t in b.tracks] == ["A x", "B y"]


def test_spotify_uri() -> None:
    b = resolve_bulk("spotify:playlist:37i9", spotify_playlist=_spotify)
    assert b.origin == "spotify" and [t.display() for t in b.tracks] == ["A x", "B y"]


def test_spotify_dict_without_query_uses_structured_fields() -> None:
    # a resolver dict that omits "query" -> identity built straight from the fields
    def sp(_r: str) -> dict:
        return {
            "name": "P",
            "owner": "",
            "tracks": [{"artist": "A", "title": "T", "isrc": "US1", "duration_s": 100}],
        }

    b = resolve_bulk("spotify:playlist:z", spotify_playlist=sp)
    assert b.tracks == [TrackIdentity("A", "T", "US1", 100)]


def test_spotify_bare_string_tracks_parse_via_from_query() -> None:
    # a resolver that only knows free text -> from_query best-effort split
    def sp(_r: str) -> dict:
        return {"name": "P", "owner": "", "tracks": ["Daft Punk - Get Lucky", "Instant Crush"]}

    b = resolve_bulk("spotify:playlist:z", spotify_playlist=sp)
    assert b.tracks[0] == TrackIdentity(artist="Daft Punk", title="Get Lucky")
    assert b.tracks[1] == TrackIdentity(title="Instant Crush")


def test_youtube_playlist_link() -> None:
    b = resolve_bulk("https://www.youtube.com/playlist?list=PL123", youtube_playlist=_youtube)
    assert b.origin == "youtube" and b.name == "YT mix"
    assert b.tracks == [TrackIdentity(title="C z")]


def test_pasted_list() -> None:
    b = resolve_bulk("Daft Punk - Get Lucky\n  Burial Archangel \n\nBonobo Kerala")
    assert b.origin == "paste"
    # "Artist - Title" splits; the others stay title-only
    assert b.tracks == [
        TrackIdentity(artist="Daft Punk", title="Get Lucky"),
        TrackIdentity(title="Burial Archangel"),
        TrackIdentity(title="Bonobo Kerala"),
    ]
    assert "3 tracks" in b.name


def test_single_line_uses_it_as_name() -> None:
    b = resolve_bulk("Aphex Twin Rhubarb")
    assert b.origin == "paste" and b.name == "Aphex Twin Rhubarb"
    assert b.tracks == [TrackIdentity(title="Aphex Twin Rhubarb")]


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


def test_youtube_playlist_with_no_tracks_is_value_error() -> None:
    with pytest.raises(ValueError):
        resolve_bulk(
            "https://youtube.com/playlist?list=x",
            youtube_playlist=lambda _r: {"name": "Empty", "tracks": []},
        )
