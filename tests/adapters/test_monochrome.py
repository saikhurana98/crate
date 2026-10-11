"""Monochrome Tracks API search and direct FLAC download."""

from pathlib import Path

import pytest

from crate.adapters.sources.monochrome import (
    monochrome_download,
    parse_search,
    select_track,
)
from crate.domain.identity import TrackIdentity

SEARCH_RESPONSE = {
    "tracks": [
        {
            "trackId": "160138174831038464",
            "title": "Zara Sa",
            "artistNames": ["Pritam", "KK"],
            "duration": 303680,
            "isrc": "INS170800033",
            "artwork": "https://tracks.monochrome.st/cover.jpg",
            "playable": True,
        },
        {
            "trackId": "197382643367886848",
            "title": "Zara Sa Jhoom Loon Main",
            "artistNames": ["Asha Bhosle", "Abhijeet"],
            "duration": 353693,
            "playable": True,
        },
        {"trackId": "3", "title": "Zara Sa", "playable": False},
    ]
}


def test_parse_search_uses_current_api_fields() -> None:
    tracks = parse_search(SEARCH_RESPONSE)
    assert len(tracks) == 2
    assert tracks[0].id == "160138174831038464"
    assert tracks[0].artist == "Pritam, KK"
    assert tracks[0].duration_s == 304
    assert tracks[0].isrc == "INS170800033"
    assert tracks[0].cover_url == "https://tracks.monochrome.st/cover.jpg"


def test_select_track_prefers_correct_artist_and_isrc() -> None:
    tracks = parse_search(SEARCH_RESPONSE)
    chosen = select_track(tracks, "KK Zara Sa", TrackIdentity("KK", "Zara Sa", duration_s=304))
    assert chosen.id == "160138174831038464"
    chosen = select_track(tracks, "unrelated query", TrackIdentity(isrc="ins170800033"))
    assert chosen.id == "160138174831038464"


def test_select_track_rejects_wrong_artist() -> None:
    with pytest.raises(LookupError):
        select_track(
            parse_search(SEARCH_RESPONSE),
            "Zara Sa",
            TrackIdentity(artist="Nobody", title="Zara Sa"),
        )


def test_download_searches_once_and_fetches_selected_id(tmp_path: Path) -> None:
    calls: list[str] = []

    def fetch(track_id: str, dest: Path) -> None:
        calls.append(track_id)
        dest.write_bytes(b"fLaC" + b"\0" * 64)

    outcome = monochrome_download(
        "KK Zara Sa",
        tmp_path,
        TrackIdentity("KK", "Zara Sa", duration_s=304),
        search=lambda query: SEARCH_RESPONSE,
        fetch=fetch,
        clock=iter([1.0, 2.0]).__next__,
    )
    assert calls == ["160138174831038464"]
    assert outcome.path.read_bytes().startswith(b"fLaC")
    assert outcome.artist == "Pritam, KK"
    assert outcome.title == "Zara Sa"
    assert not list(tmp_path.glob("*.part"))


def test_non_flac_response_is_rejected_and_removed(tmp_path: Path) -> None:
    def fetch(track_id: str, dest: Path) -> None:
        dest.write_bytes(b"<html>error</html>")

    with pytest.raises(ValueError, match="non-FLAC"):
        monochrome_download("Zara Sa", tmp_path, search=lambda query: SEARCH_RESPONSE, fetch=fetch)
    assert not list(tmp_path.iterdir())
