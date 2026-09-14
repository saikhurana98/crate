"""Matching a Spotify track to a Navidrome song."""

from __future__ import annotations

from typing import Any

from crate.domain.navidrome import pick_song, search_query

TRACK = {"artist": "Daft Punk", "title": "Get Lucky", "isrc": "gbduw1300001", "duration_s": 248}


def _song(sid: str, **kw: Any) -> dict[str, Any]:
    return {"id": sid, "artist": "Daft Punk", "title": "Get Lucky", "duration": 248} | kw


def test_search_query() -> None:
    assert search_query(TRACK) == "Daft Punk Get Lucky"
    assert search_query({"title": "Solo"}) == "Solo"


def test_isrc_wins_even_when_names_differ() -> None:
    songs = [_song("a"), _song("b", title="Get Lucky (Radio Edit)", isrc=["GBDUW1300001"])]
    assert pick_song(TRACK, songs) == "b"
    assert pick_song(TRACK, [_song("c", title="x", isrc="GBDUW1300001")]) == "c"


def test_name_match_uses_the_merge_key_and_any_credited_artist() -> None:
    feat = _song("f", artist="Pharrell Williams & Daft Punk", title="Get Lucky (feat. Pharrell)")
    feat["artists"] = [{"name": "Pharrell Williams"}, {"name": "Daft Punk"}]
    assert pick_song({"artist": "Daft Punk", "title": "Get Lucky"}, [feat]) == "f"
    assert pick_song(TRACK, [_song("x", title="Around the World")]) is None


def test_durations_far_apart_are_different_recordings() -> None:
    assert pick_song(TRACK, [_song("long", duration=369)]) is None
    assert pick_song(TRACK, [_song("near", duration=251)]) == "near"
    assert (
        pick_song({"artist": "Daft Punk", "title": "Get Lucky"}, [_song("n", duration=369)]) == "n"
    )


def test_prefers_lossless_then_bitrate() -> None:
    songs = [
        _song("mp3", suffix="mp3", bitRate=320),
        _song("flac", suffix="flac", bitRate=900),
        _song("flac2", suffix="FLAC", bitRate=1100),
    ]
    assert pick_song({"artist": "Daft Punk", "title": "Get Lucky"}, songs) == "flac2"


def test_nothing_to_match_on() -> None:
    assert pick_song({}, [_song("a", artist="", title="")]) is None
    assert pick_song(TRACK, [{"title": "Get Lucky"}]) is None  # no id
