from __future__ import annotations

from pathlib import Path

import pytest

from crate.adapters.sources import octave
from crate.domain.identity import TrackIdentity


def _search(_query: str) -> dict:
    return {
        "tracks": [
            {
                "id": "4350411742",
                "title": "Housewife",
                "artist": {"name": "Tate McRae"},
                "album": {
                    "title": "Housewife",
                    "cover_xl": "https://img.example/housewife.jpg",
                },
                "duration": 208,
            },
            {"id": "bad", "title": "skip", "artist": {"name": "Artist"}},
            "invalid",
        ]
    }


def test_search_candidates_preserve_catalog_metadata() -> None:
    candidates = octave.search_candidates("Housewife", search=_search)
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.source == "octave" and candidate.ref == "4350411742"
    assert candidate.artist == "Tate McRae" and candidate.album == "Housewife"
    assert candidate.duration_s == 208 and candidate.lossless
    assert candidate.art_url == "https://img.example/housewife.jpg"


def test_structured_identity_filters_results() -> None:
    want = TrackIdentity("Tate McRae", "Housewife", duration_s=208)
    assert octave.search_candidates("ignored", want, search=_search)[0].ref == "4350411742"
    assert (
        octave.search_candidates(
            "ignored", TrackIdentity("Other Artist", "Housewife"), search=_search
        )
        == []
    )


def test_download_candidate_checks_flac_and_preserves_metadata(tmp_path: Path) -> None:
    candidate = octave.search_candidates("Housewife", search=_search)[0]

    def fetch(track_id: str, dest: Path) -> None:
        assert track_id == "4350411742"
        dest.write_bytes(b"fLaC\x00audio")

    outcome = octave.download_candidate(
        candidate, tmp_path, fetch=fetch, clock=iter([2.0, 4.5]).__next__
    )
    assert outcome.source == "octave" and outcome.elapsed_s == 2.5
    assert outcome.path.read_bytes().startswith(b"fLaC")
    assert outcome.album == "Housewife" and outcome.art_url

    def bad_fetch(_track_id: str, dest: Path) -> None:
        dest.write_bytes(b"not audio")

    with pytest.raises(ValueError, match="non-FLAC"):
        octave.download_candidate(candidate, tmp_path, fetch=bad_fetch)


def test_automatic_download_selects_matching_track(tmp_path: Path) -> None:
    def fetch(_track_id: str, dest: Path) -> None:
        dest.write_bytes(b"fLaC")

    outcome = octave.octave_download(
        "Tate McRae - Housewife",
        tmp_path,
        TrackIdentity("Tate McRae", "Housewife", duration_s=208),
        search=_search,
        fetch=fetch,
    )
    assert outcome.title == "Housewife" and outcome.artist == "Tate McRae"

    with pytest.raises(LookupError, match="matching Octave"):
        octave.octave_download("Unknown", tmp_path, search=_search, fetch=fetch)
