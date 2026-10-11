"""Review-tab candidate surface for every candidate-capable adapter.

Each source gains ``search_candidates`` (return ALL title-matching results as
Candidates, no select-collapse) and ``download_candidate`` (fetch exactly one
result by its ``ref``, skipping search/select). All IO is injected, so these are
tested with fakes and assert the download-by-ref path never re-searches.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from crate.adapters.sources import arcod, deezer, monochrome, qobuz, ytdlp
from crate.domain.candidate import Candidate

# --- qobuz --------------------------------------------------------------------
QOBUZ_SEARCH = {
    "tracks": {
        "items": [
            {
                "id": 12345,
                "title": "Get Lucky",
                "performer": {"name": "Daft Punk"},
                "album": {"title": "Random Access Memories"},
                "duration": 369,
                "maximum_bit_depth": 24,
                "maximum_sampling_rate": 88.2,
                "isrc": "USQX91300108",
            },
            {  # different title -> filtered out by the title gate
                "id": 999,
                "title": "Completely Different Song",
                "performer": {"name": "Someone"},
                "album": {"title": "X"},
                "duration": 100,
                "maximum_bit_depth": 16,
                "maximum_sampling_rate": 44.1,
                "isrc": None,
            },
        ]
    }
}


def test_qobuz_search_candidates_returns_all_title_matches() -> None:
    cands = qobuz.search_candidates("Daft Punk Get Lucky", search=lambda q: QOBUZ_SEARCH)
    assert len(cands) == 1
    c = cands[0]
    assert c.source == "qobuz" and c.tier == 1 and c.ref == "12345"
    assert c.title == "Get Lucky" and c.artist == "Daft Punk"
    assert c.lossless is True and c.quality_label == "FLAC 24/88.2"
    assert c.duration_s == 369


def test_qobuz_download_candidate_fetches_by_ref_no_search(tmp_path: Path) -> None:
    picked: dict[str, int] = {}

    def get_file_url(track_id: int) -> str:
        picked["id"] = track_id
        return "https://cdn.qobuz/track.flac"

    def fetch(url: str, dest: Path) -> None:
        dest.write_bytes(b"fLaC\x00x")

    cand = Candidate("qobuz", 1, "12345", "Get Lucky", "Daft Punk", "RAM", "FLAC", True, 369, None)
    outcome = qobuz.download_candidate(
        cand, tmp_path, get_file_url=get_file_url, fetch=fetch, clock=iter([0.0, 1.0]).__next__
    )
    assert picked["id"] == 12345  # fetched exactly the ref, no search step
    assert outcome.source == "qobuz" and outcome.path.suffix == ".flac"
    assert outcome.title == "Get Lucky" and outcome.artist == "Daft Punk"


# --- deezer -------------------------------------------------------------------
DEEZER_SEARCH = {
    "data": [
        {
            "id": 555,
            "title": "Made to Stray",
            "artist": {"name": "Mount Kimbie"},
            "album": {"title": "Cold Spring Fault Less Youth", "cover_xl": "x"},
            "duration": 237,
            "isrc": "GB1",
        },
        {
            "id": 556,
            "title": "Unrelated",
            "artist": {"name": "Nobody"},
            "album": {"title": "Y"},
            "duration": 100,
            "isrc": None,
        },
    ]
}


def test_deezer_search_candidates_filters_by_title() -> None:
    cands = deezer.search_candidates("Mount Kimbie Made to Stray", search=lambda q: DEEZER_SEARCH)
    assert [c.ref for c in cands] == ["555"]
    assert cands[0].tier == 2 and cands[0].lossless is True
    assert cands[0].art_url == "x"  # album cover flows onto the candidate


def test_deezer_download_candidate_by_ref(tmp_path: Path) -> None:
    picked: dict[str, int] = {}

    def resolve_url(track_id: int) -> str:
        picked["id"] = track_id
        return "https://cdn/enc"

    def fetch(url: str, dest: Path) -> None:
        dest.write_bytes(b"\x00" * 2048)  # cleartext under the stripe rule

    cand = Candidate(
        "deezer", 2, "555", "Made to Stray", "Mount Kimbie", None, "FLAC", True, 237, None
    )
    outcome = deezer.download_candidate(
        cand, tmp_path, resolve_url=resolve_url, fetch=fetch, clock=iter([0.0, 1.0]).__next__
    )
    assert picked["id"] == 555 and outcome.source == "deezer"


# --- monochrome ---------------------------------------------------------------
MONO_SEARCH = {
    "tracks": [
        {
            "trackId": "700",
            "title": "Rhubarb",
            "artistNames": ["Aphex Twin"],
            "albumTitle": "Selected Ambient Works",
            "artwork": "https://example.test/cover.jpg",
            "duration": 451000,
            "playable": True,
        },
        {"trackId": "701", "title": "Something Else", "artistNames": ["Other"]},
    ]
}


def test_monochrome_search_candidates() -> None:
    cands = monochrome.search_candidates("Aphex Twin Rhubarb", search=lambda q: MONO_SEARCH)
    assert [c.ref for c in cands] == ["700"]
    assert cands[0].source == "monochrome" and cands[0].quality_label == "FLAC"


def test_monochrome_download_candidate_fetches_ref_without_search(tmp_path: Path) -> None:
    seen: dict[str, object] = {}

    def fetch(track_id: str, out: Path) -> None:
        seen["id"] = track_id
        out.write_bytes(b"fLaC\x00")

    cand = Candidate(
        "monochrome", 1, "700", "Rhubarb", "Aphex Twin", "SAW", "FLAC", True, 451, None
    )
    outcome = monochrome.download_candidate(
        cand,
        tmp_path,
        fetch=fetch,
        clock=iter([0.0, 1.0]).__next__,
    )
    assert seen["id"] == "700"
    assert outcome.source == "monochrome" and outcome.path.suffix == ".flac"


# --- arcod --------------------------------------------------------------------
ARCOD_TRACK = {
    "id": 88,
    "title": "Gosh",
    "performer": {"name": "Jamie xx", "id": "p1"},
    "album": {
        "id": "al1",
        "title": "In Colour",
        "image": {"large": "cover"},
        "release_date_original": "2015-05-29",
    },
    "isrc": "GB2",
    "duration": 311,
    "track_number": 1,
    "hires": True,
}
ARCOD_SEARCH = {"success": True, "data": {"tracks": {"items": [ARCOD_TRACK]}}}


def test_arcod_search_candidates_packs_payload_into_ref() -> None:
    cands = arcod.search_candidates("Jamie xx Gosh", search=lambda q: ARCOD_SEARCH)
    assert len(cands) == 1
    payload = json.loads(cands[0].ref)  # ref is the full download payload
    assert payload["trackId"] == "88" and payload["albumId"] == "al1"
    assert cands[0].lossless is False  # arcod may downgrade -> honest


def test_arcod_download_candidate_replays_payload_no_search(tmp_path: Path) -> None:
    sent: dict[str, object] = {}

    def create_download(payload: dict[str, object]) -> dict[str, object]:
        sent["payload"] = payload
        return {"id": "job1", "accessToken": "tok"}

    def poll(job_id: str, token: str) -> dict[str, object]:
        return {"status": "completed", "downloadUrl": "https://d/f.flac", "fileName": "f.flac"}

    def fetch(url: str, dest: Path) -> None:
        dest.write_bytes(b"fLaC\x00")

    cand = arcod.search_candidates("Jamie xx Gosh", search=lambda q: ARCOD_SEARCH)[0]
    outcome = arcod.download_candidate(
        cand,
        tmp_path,
        create_download=create_download,
        poll=poll,
        fetch=fetch,
        clock=iter([0.0, 0.0, 1.0]).__next__,
        sleep=lambda s: None,
    )
    assert sent["payload"]["trackId"] == "88"  # type: ignore[index]
    assert outcome.source == "arcod"


# --- ytdlp --------------------------------------------------------------------
def _cp(returncode: int, stdout: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["yt-dlp"], returncode=returncode, stdout=stdout, stderr=""
    )


def test_ytdlp_search_candidates_parses_and_filters() -> None:
    rows = "vid1\tRhubarb (Official Audio)\tAphex Twin - Topic\t451\nvid2\tUnrelated\tX\t100\n"

    def runner(cmd: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
        assert "--skip-download" in cmd  # metadata-only probe
        return _cp(0, rows)

    cands = ytdlp.search_candidates("Aphex Twin Rhubarb", runner=runner)
    assert [c.ref for c in cands] == ["vid1"]
    assert cands[0].source == "ytdlp" and cands[0].tier == 3 and cands[0].lossless is False


def test_ytdlp_search_candidates_empty_on_search_failure() -> None:
    assert ytdlp.search_candidates("x", runner=lambda c, t: _cp(1)) == []


def test_ytdlp_download_candidate_by_video_id_no_search(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def runner(cmd: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        return _cp(0, str(tmp_path / "out.opus"))

    cand = Candidate(
        "ytdlp", 3, "vid1", "Rhubarb", "Aphex Twin", None, "opus ~160", False, 451, None
    )
    outcome = ytdlp.download_candidate(
        cand, tmp_path, runner=runner, clock=iter([0.0, 1.0]).__next__
    )
    # exactly one runner call — the download-by-id, no ytsearch probe
    assert len(calls) == 1
    assert any("watch?v=vid1" in a for a in calls[0])
    assert outcome.source == "ytdlp" and outcome.title == "Rhubarb"
