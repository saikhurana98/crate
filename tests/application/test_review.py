"""run_review_download use-case: targeted re-download → upgrade-in-place / fulfil.

All collaborators injected (download / prober / finalizer / replace / recorder),
so both branches are tested without a DB, network, or real files."""

from __future__ import annotations

from pathlib import Path

import pytest

from crate.application.download import DownloadReport
from crate.application.review import run_review_download
from crate.domain.audio import AudioInfo
from crate.domain.candidate import Candidate
from crate.domain.download import DownloadOutcome


class _Recorder:
    def __init__(self) -> None:
        self.attempts: list[tuple[str, str, int, bool, str]] = []
        self.finished: list[tuple[str, DownloadReport]] = []

    def record_attempt(self, rid: str, source: str, tier: int, ok: bool, detail: str) -> None:
        self.attempts.append((rid, source, tier, ok, detail))

    def finish_request(self, rid: str, report: DownloadReport) -> None:
        self.finished.append((rid, report))


def _info(codec: str) -> AudioInfo:
    return AudioInfo(codec, codec, 44100, 2, 16, 900_000, 200.0, 5_000_000)


def _cand(source: str = "qobuz", tier: int = 1) -> Candidate:
    return Candidate(source, tier, "42", "Rhubarb", "Aphex Twin", "SAW", "FLAC", True, 451, None)


def test_upgrade_replaces_in_place_and_finishes_at_target(tmp_path: Path) -> None:
    produced = tmp_path / "new.flac"
    recorder = _Recorder()
    moves: list[tuple[Path, Path]] = []

    def download(c: Candidate, out_dir: Path, want: object) -> DownloadOutcome:
        produced.write_bytes(b"fLaC")
        return DownloadOutcome(produced, c.tier, c.source, 2.0, "q")

    final = run_review_download(
        "req1",
        tmp_path,
        _cand(),
        want=None,
        is_upgrade=True,
        target_path="/nas/lib/old.opus",
        download=download,
        prober=lambda p: _info("flac"),
        finalizer=None,
        replace=lambda src, dst: moves.append((src, dst)),
        recorder=recorder,
    )

    assert final == Path("/nas/lib/old.opus")
    assert moves == [(produced, Path("/nas/lib/old.opus"))]  # replaced in place
    rid, report = recorder.finished[0]
    assert rid == "req1"
    assert report.outcome is not None and report.outcome.path == Path("/nas/lib/old.opus")
    assert recorder.attempts[0][4].startswith("review: replaced")


def test_fulfil_failed_request_finalizes_into_library(tmp_path: Path) -> None:
    produced = tmp_path / "new.flac"
    recorder = _Recorder()

    def download(c: Candidate, out_dir: Path, want: object) -> DownloadOutcome:
        produced.write_bytes(b"fLaC")
        return DownloadOutcome(produced, c.tier, c.source, 2.0, "q")

    def finalizer(report: DownloadReport) -> DownloadReport:
        assert report.outcome is not None
        moved = report.outcome.path.with_name("filed.flac")
        import dataclasses

        return dataclasses.replace(report, outcome=dataclasses.replace(report.outcome, path=moved))

    final = run_review_download(
        "req2",
        tmp_path,
        _cand(),
        want=None,
        is_upgrade=False,
        target_path=None,
        download=download,
        prober=lambda p: _info("flac"),
        finalizer=finalizer,
        replace=lambda src, dst: None,
        recorder=recorder,
    )

    assert final.name == "filed.flac"  # went through the librarian
    assert recorder.finished[0][0] == "req2"
    assert recorder.attempts[0][4].startswith("review: downloaded")


def test_download_failure_propagates_for_caller_to_fail(tmp_path: Path) -> None:
    recorder = _Recorder()

    def download(c: Candidate, out_dir: Path, want: object) -> DownloadOutcome:
        raise RuntimeError("source down")

    with pytest.raises(RuntimeError, match="source down"):
        run_review_download(
            "req3",
            tmp_path,
            _cand(),
            want=None,
            is_upgrade=False,
            target_path=None,
            download=download,
            prober=lambda p: _info("flac"),
            finalizer=None,
            replace=lambda src, dst: None,
            recorder=recorder,
        )
    assert recorder.finished == []  # nothing finished; caller marks it failed
