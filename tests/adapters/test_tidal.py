"""TIDAL catalog selection, candidates, metadata, and FLAC validation."""

from pathlib import Path

import pytest

from crate.adapters.sources.tidal import parse_search, select_track, tidal_download
from crate.domain.identity import TrackIdentity

SEARCH_RESPONSE = {
    "items": [
        {
            "id": 562641091,
            "title": "Ashke",
            "duration": 218,
            "isrc": "GXLEL2633343",
            "audioQuality": "HI_RES_LOSSLESS",
            "trackNumber": 1,
            "volumeNumber": 1,
            "artists": [{"name": "Karan Aujla"}],
            "album": {
                "title": "AUJLA SZN 1",
                "cover": "5cdec76e-e6f5-4452-9f1d-18bee6f7c30e",
            },
        },
        {
            "id": 1,
            "title": "Ashke (Cover)",
            "duration": 200,
            "artists": [{"name": "Someone Else"}],
            "album": {"title": "Covers"},
        },
    ]
}


def test_parse_search_preserves_library_metadata() -> None:
    track = parse_search(SEARCH_RESPONSE)[0]
    assert track.id == "562641091"
    assert track.artist == "Karan Aujla"
    assert track.album == "AUJLA SZN 1"
    assert track.track_no == 1 and track.disc_no == 1
    assert track.cover_url == (
        "https://resources.tidal.com/images/5cdec76e/e6f5/4452/9f1d/18bee6f7c30e/1280x1280.jpg"
    )


def test_select_track_prefers_exact_isrc_and_rejects_wrong_artist() -> None:
    tracks = parse_search(SEARCH_RESPONSE)
    chosen = select_track(tracks, "anything", TrackIdentity(isrc="gxlel2633343"))
    assert chosen.id == "562641091"
    with pytest.raises(LookupError):
        select_track(tracks, "Ashke", TrackIdentity("Nobody", "Ashke"))


def test_download_returns_complete_metadata_and_removes_part(tmp_path: Path) -> None:
    def fetch(track_id: str, dest: Path) -> None:
        assert track_id == "562641091"
        dest.write_bytes(b"fLaC" + b"\0" * 32)

    outcome = tidal_download(
        "Karan Aujla Ashke",
        tmp_path,
        TrackIdentity("Karan Aujla", "Ashke", "GXLEL2633343", 218),
        search=lambda query: SEARCH_RESPONSE,
        fetch=fetch,
        clock=iter([3.0, 5.0]).__next__,
    )
    assert outcome.path.read_bytes().startswith(b"fLaC")
    assert outcome.source == "tidal"
    assert outcome.album == "AUJLA SZN 1"
    assert outcome.track_no == 1 and outcome.disc_no == 1
    assert outcome.art_url and "resources.tidal.com" in outcome.art_url
    assert not list(tmp_path.glob("*.part"))


def test_non_flac_result_is_rejected(tmp_path: Path) -> None:
    def fetch(track_id: str, dest: Path) -> None:
        dest.write_bytes(b"not audio")

    with pytest.raises(ValueError, match="non-FLAC"):
        tidal_download("Ashke", tmp_path, search=lambda query: SEARCH_RESPONSE, fetch=fetch)
    assert not list(tmp_path.iterdir())
