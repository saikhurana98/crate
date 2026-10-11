from __future__ import annotations

import json
from pathlib import Path

import pytest

from crate.adapters.sources import antra
from crate.domain.identity import TrackIdentity


def test_search_candidate_preserves_structured_identity() -> None:
    want = TrackIdentity("Karan Aujla", "Ashke", "GXLEL2633343", 218)
    candidates = antra.search_candidates("ignored", want)

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.source == "antra" and candidate.tier == 1 and candidate.lossless
    assert candidate.quality_label == "FLAC 16-bit via Antra"
    assert json.loads(candidate.ref) == {
        "album": "",
        "artists": ["Karan Aujla"],
        "disc_number": None,
        "duration_ms": 218000,
        "isrc": "GXLEL2633343",
        "release_year": None,
        "title": "Ashke",
        "track_number": None,
    }


def test_search_requires_artist_and_supports_artist_dash_title() -> None:
    assert antra.search_candidates("title only") == []
    candidate = antra.search_candidates("Aphex Twin - Rhubarb")[0]
    assert (candidate.artist, candidate.title) == ("Aphex Twin", "Rhubarb")


def test_download_candidate_uses_exact_metadata_and_checks_flac(tmp_path: Path) -> None:
    candidate = antra.search_candidates(
        "ignored", TrackIdentity("Aphex Twin", "Rhubarb", "GB123", 451)
    )[0]
    seen: dict[str, object] = {}

    def download(track: dict[str, object], dest: Path) -> None:
        seen.update(track)
        dest.write_bytes(b"fLaC\x00audio")

    outcome = antra.download_candidate(
        candidate,
        tmp_path,
        download=download,
        clock=iter([1.0, 3.5]).__next__,
    )
    assert seen["isrc"] == "GB123"
    assert outcome.path.read_bytes().startswith(b"fLaC")
    assert outcome.source == "antra" and outcome.elapsed_s == 2.5

    def bad_download(track: dict[str, object], dest: Path) -> None:
        dest.write_bytes(b"<!doctype html>")

    with pytest.raises(ValueError, match="non-FLAC"):
        antra.download_candidate(candidate, tmp_path, download=bad_download)


def test_automatic_download_builds_candidate(tmp_path: Path) -> None:
    def download(track: dict[str, object], dest: Path) -> None:
        assert track["artists"] == ["Artist"]
        dest.write_bytes(b"fLaC")

    outcome = antra.antra_download("Artist - Song", tmp_path, download=download)
    assert outcome.title == "Song" and outcome.artist == "Artist"

    with pytest.raises(LookupError, match="artist and title"):
        antra.antra_download("Song", tmp_path, download=download)
