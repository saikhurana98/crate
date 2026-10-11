"""run_review_download use-case: targeted re-download → upgrade-in-place / fulfil.

All collaborators injected (download / prober / finalizer / replace / recorder),
so both branches are tested without a DB, network, or real files."""

from __future__ import annotations

from pathlib import Path

import pytest

from crate.application.download import DownloadReport
from crate.application.review import MatchTooLow, handle_review_error, run_review_download
from crate.domain.audio import AudioInfo
from crate.domain.candidate import Candidate
from crate.domain.download import DownloadOutcome
from crate.domain.fingerprint import similarity

_FP = [11, 22, 33, 44, 55, 66, 77, 88, 99, 111, 122, 133]  # a stand-in raw fingerprint
_FP_OTHER = [x ^ 0xFFFFFFFF for x in _FP]  # every bit differs -> similarity 0.0


class _Recorder:
    def __init__(self, restore_ok: bool = True) -> None:
        self.attempts: list[tuple[str, str, int, bool, str]] = []
        self.finished: list[tuple[str, DownloadReport]] = []
        self.similarities: list[tuple[str, float]] = []
        self.restored: list[str] = []
        self.failed: list[tuple[str, str]] = []
        self.restore_ok = restore_ok

    def record_attempt(self, rid: str, source: str, tier: int, ok: bool, detail: str) -> None:
        self.attempts.append((rid, source, tier, ok, detail))

    def finish_request(self, rid: str, report: DownloadReport) -> None:
        self.finished.append((rid, report))

    def set_match_similarity(self, rid: str, score: float) -> None:
        self.similarities.append((rid, score))

    def restore_completed(self, rid: str) -> bool:
        self.restored.append(rid)
        return self.restore_ok

    def fail_request(self, rid: str, error: str) -> None:
        self.failed.append((rid, error))


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
    assert recorder.attempts[0][4].startswith("review: finalized")


def test_upgrade_finalizes_metadata_and_removes_old_lossy_path(tmp_path: Path) -> None:
    produced = tmp_path / "new.flac"
    existing = tmp_path / "Unknown Album" / "old.opus"
    existing.parent.mkdir()
    existing.write_bytes(b"OggS")
    filed = tmp_path / "Artist" / "Album" / "01 Song.flac"
    recorder = _Recorder()

    def download(c: Candidate, out_dir: Path, want: object) -> DownloadOutcome:
        produced.write_bytes(b"fLaC")
        return DownloadOutcome(produced, c.tier, c.source, 2.0, "q", art_url="cover")

    def finalizer(report: DownloadReport) -> DownloadReport:
        import dataclasses

        assert report.outcome is not None and report.outcome.art_url == "cover"
        filed.parent.mkdir(parents=True)
        report.outcome.path.replace(filed)
        return dataclasses.replace(report, outcome=dataclasses.replace(report.outcome, path=filed))

    final = run_review_download(
        "req-meta",
        tmp_path,
        _cand(),
        want=None,
        is_upgrade=True,
        target_path=str(existing),
        download=download,
        prober=lambda p: _info("flac"),
        finalizer=finalizer,
        replace=lambda src, dst: src.replace(dst),
        recorder=recorder,
    )

    assert final == filed and filed.exists()
    assert not existing.exists()
    assert recorder.finished[0][1].outcome is not None
    assert recorder.finished[0][1].outcome.path == filed


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


# --- fingerprint gate (ADR-0019) ---


def _gate_download(produced: Path):  # type: ignore[no-untyped-def]
    def download(c: Candidate, out_dir: Path, want: object) -> DownloadOutcome:
        produced.write_bytes(b"fLaC")
        return DownloadOutcome(produced, c.tier, c.source, 2.0, "q")

    return download


def _fps(produced: Path, existing: Path, new_fp: list[int], old_fp: list[int]):  # type: ignore[no-untyped-def]
    def fingerprinter(path: Path) -> tuple[int, list[int]]:
        return (200, new_fp) if path == produced else (200, old_fp) if path == existing else (0, [])

    return fingerprinter


def test_gate_passes_when_fingerprints_match(tmp_path: Path) -> None:
    produced, existing = tmp_path / "new.flac", tmp_path / "old.opus"
    existing.write_bytes(b"OggS")
    recorder = _Recorder()
    moves: list[tuple[Path, Path]] = []

    run_review_download(
        "r",
        tmp_path,
        _cand(),
        want=None,
        is_upgrade=True,
        target_path=str(existing),
        download=_gate_download(produced),
        prober=lambda p: _info("flac"),
        finalizer=None,
        replace=lambda s, d: moves.append((s, d)),
        recorder=recorder,
        fingerprinter=_fps(produced, existing, _FP, _FP),
        similarity=similarity,
    )
    assert moves == [(produced, existing)]  # replaced
    assert recorder.similarities == [("r", 1.0)]


def test_gate_blocks_and_discards_when_fingerprints_differ(tmp_path: Path) -> None:
    produced, existing = tmp_path / "new.flac", tmp_path / "old.opus"
    existing.write_bytes(b"OggS")
    recorder = _Recorder()
    moves: list[tuple[Path, Path]] = []

    with pytest.raises(MatchTooLow) as exc:
        run_review_download(
            "r",
            tmp_path,
            _cand(),
            want=None,
            is_upgrade=True,
            target_path=str(existing),
            download=_gate_download(produced),
            prober=lambda p: _info("flac"),
            finalizer=None,
            replace=lambda s, d: moves.append((s, d)),
            recorder=recorder,
            fingerprinter=_fps(produced, existing, _FP, _FP_OTHER),
            similarity=similarity,
        )
    assert exc.value.score < 0.5  # clearly a mismatch
    assert moves == []  # existing file untouched
    assert not produced.exists()  # rejected download discarded
    assert recorder.finished == []


def test_force_skips_the_gate(tmp_path: Path) -> None:
    produced, existing = tmp_path / "new.flac", tmp_path / "old.opus"
    existing.write_bytes(b"OggS")
    recorder = _Recorder()
    moves: list[tuple[Path, Path]] = []

    run_review_download(
        "r",
        tmp_path,
        _cand(),
        want=None,
        is_upgrade=True,
        target_path=str(existing),
        download=_gate_download(produced),
        prober=lambda p: _info("flac"),
        finalizer=None,
        replace=lambda s, d: moves.append((s, d)),
        recorder=recorder,
        fingerprinter=_fps(produced, existing, _FP, _FP_OTHER),  # would fail the gate
        similarity=similarity,
        force=True,
    )
    assert moves == [(produced, existing)]  # replaced anyway
    assert recorder.similarities == []  # gate not run, no score recorded


def test_no_fingerprinter_skips_the_gate(tmp_path: Path) -> None:
    produced, existing = tmp_path / "new.flac", tmp_path / "old.opus"
    existing.write_bytes(b"OggS")
    recorder = _Recorder()
    moves: list[tuple[Path, Path]] = []

    run_review_download(
        "r",
        tmp_path,
        _cand(),
        want=None,
        is_upgrade=True,
        target_path=str(existing),
        download=_gate_download(produced),
        prober=lambda p: _info("flac"),
        finalizer=None,
        replace=lambda s, d: moves.append((s, d)),
        recorder=recorder,
        fingerprinter=None,  # binary absent -> feature off
        similarity=similarity,
    )
    assert moves == [(produced, existing)]


# --- handle_review_error: graceful recovery on a source/network failure ---
def test_review_error_upgrade_keeps_existing_and_restores_done(tmp_path: Path) -> None:
    """A transient source failure during an UPGRADE must not fail the request: the
    existing file is untouched, so record the attempt and restore it to done."""
    existing = tmp_path / "old.opus"
    existing.write_bytes(b"OggS")
    recorder = _Recorder()

    handle_review_error(
        "req1",
        _cand("qobuz"),
        RuntimeError("401 Unauthorized"),
        is_upgrade=True,
        target_path=str(existing),
        recorder=recorder,
        exists=lambda p: p.exists(),
    )

    assert recorder.restored == ["req1"]
    assert recorder.failed == []  # not marked failed — the good lossy file stays
    assert recorder.attempts == [("req1", "qobuz", 1, False, "review: 401 Unauthorized")]


def test_review_error_fulfil_fails_with_clean_message(tmp_path: Path) -> None:
    """A failure while FULFILLING a failed request (no existing file) marks it failed
    with a clean, attributed message — not an opaque 'queue worker error'."""
    recorder = _Recorder()
    handle_review_error(
        "req2",
        _cand("qobuz"),
        RuntimeError("timeout"),
        is_upgrade=False,
        target_path=None,
        recorder=recorder,
        exists=lambda p: False,
    )
    assert recorder.restored == []
    assert recorder.failed == [("req2", "review download from qobuz failed: timeout")]


def test_review_error_upgrade_missing_file_falls_back_to_fail(tmp_path: Path) -> None:
    """If the existing upgrade target has vanished, there's nothing to keep — fail."""
    recorder = _Recorder()
    handle_review_error(
        "req3",
        _cand("qobuz"),
        RuntimeError("boom"),
        is_upgrade=True,
        target_path=str(tmp_path / "gone.opus"),
        recorder=recorder,
        exists=lambda p: p.exists(),
    )
    assert recorder.restored == []
    assert recorder.failed[0][0] == "req3"


def test_review_error_upgrade_restore_noop_falls_back_to_fail(tmp_path: Path) -> None:
    """If restore_completed can't restore (no download row), fail cleanly."""
    existing = tmp_path / "old.opus"
    existing.write_bytes(b"OggS")
    recorder = _Recorder(restore_ok=False)
    handle_review_error(
        "req4",
        _cand("qobuz"),
        RuntimeError("401"),
        is_upgrade=True,
        target_path=str(existing),
        recorder=recorder,
        exists=lambda p: p.exists(),
    )
    assert recorder.restored == ["req4"]  # attempted
    assert recorder.failed[0][0] == "req4"  # but fell back to fail
