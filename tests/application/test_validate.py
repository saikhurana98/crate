"""Source credential validation use-case (injected search/download/prober)."""

from pathlib import Path

from crate.application.validate import validate_source
from crate.domain.audio import AudioInfo
from crate.domain.candidate import Candidate


def _cand(source: str = "qobuz", title: str = "Get Lucky") -> Candidate:
    return Candidate(source, 1, "1", title, "Daft Punk", "RAM", "FLAC 16/44", True, 248, None)


def _info() -> AudioInfo:
    return AudioInfo("flac", "flac", 44100, 2, 16, 900_000, 248.0, 5_000_000)


def test_validate_ok_downloads_probes_and_cleans_up(tmp_path: Path) -> None:
    produced = tmp_path / "test.flac"

    def search(q: str, want: object) -> list[Candidate]:
        return [_cand()]

    def download(c: Candidate, out_dir: Path, want: object) -> object:
        produced.write_bytes(b"fLaC")
        return type("O", (), {"path": produced})()

    res = validate_source(
        search=search, download=download, prober=lambda p: _info(), out_dir=tmp_path
    )
    assert res.ok is True
    assert "flac" in res.detail.lower()
    assert res.quality_label == "FLAC 16/44"
    assert not produced.exists()  # probe file deleted


def test_validate_reports_no_results(tmp_path: Path) -> None:
    res = validate_source(
        search=lambda q, w: [],
        download=lambda c, o, w: None,
        prober=lambda p: _info(),
        out_dir=tmp_path,
    )
    assert res.ok is False and "no results" in res.detail


def test_validate_catches_auth_error(tmp_path: Path) -> None:
    def search(q: str, want: object) -> list[Candidate]:
        raise RuntimeError("Client error '401 Unauthorized'")

    res = validate_source(
        search=search, download=lambda c, o, w: None, prober=lambda p: _info(), out_dir=tmp_path
    )
    assert res.ok is False and "401" in res.detail


def test_validate_catches_download_failure_and_still_reports(tmp_path: Path) -> None:
    def download(c: Candidate, out_dir: Path, want: object) -> object:
        raise RuntimeError("getFileUrl failed: token invalid")

    res = validate_source(
        search=lambda q, w: [_cand()],
        download=download,
        prober=lambda p: _info(),
        out_dir=tmp_path,
    )
    assert res.ok is False and "token invalid" in res.detail


def test_validate_picks_best_matching_candidate(tmp_path: Path) -> None:
    seen: dict[str, Candidate] = {}

    def download(c: Candidate, out_dir: Path, want: object) -> object:
        seen["c"] = c
        (tmp_path / "x").write_bytes(b"x")
        return type("O", (), {"path": tmp_path / "x"})()

    cands = [_cand(title="Get Lucky (Live Remix)"), _cand(title="Get Lucky")]
    validate_source(
        search=lambda q, w: cands, download=download, prober=lambda p: _info(), out_dir=tmp_path
    )
    assert seen["c"].title == "Get Lucky"  # exact title beats the remix
