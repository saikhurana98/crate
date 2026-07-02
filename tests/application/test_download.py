"""Download orchestration: query -> adapter -> probe -> quality verdict.

Fakes stand in for the yt-dlp adapter and the ffprobe wrapper, so no network
or subprocess runs in unit tests.
"""

from pathlib import Path

from lbmd.adapters.sources.ytdlp import DownloadOutcome
from lbmd.application.download import acquire, acquire_many
from lbmd.domain.audio import AudioInfo


def _outcome(path: str) -> DownloadOutcome:
    return DownloadOutcome(Path(path), tier=3, source="ytdlp", elapsed_s=2.0, query="q")


def _info(codec: str = "opus") -> AudioInfo:
    return AudioInfo(codec, "webm", 48000, 2, None, 160000, 200.0, 4_000_000)


def test_acquire_success_attaches_probe_and_verdict() -> None:
    report = acquire(
        "Diljit Dealer",
        Path("/out"),
        downloader=lambda q, d: _outcome("/out/Dealer.opus"),
        prober=lambda p: _info("opus"),
    )
    assert report.ok
    assert report.outcome is not None
    assert report.info is not None
    assert report.verdict is not None
    assert report.verdict.lossless is False
    assert report.error is None


def test_acquire_captures_download_failure() -> None:
    def boom(q: str, d: Path) -> DownloadOutcome:
        raise RuntimeError("yt-dlp failed (1): not found")

    report = acquire("nonexistent", Path("/out"), downloader=boom, prober=lambda p: _info())
    assert report.ok is False
    assert report.outcome is None
    assert report.error is not None
    assert "not found" in report.error


def test_acquire_many_preserves_order_and_mixes_outcomes() -> None:
    queries = ["good", "bad"]

    def dl(q: str, d: Path) -> DownloadOutcome:
        if q == "bad":
            raise RuntimeError("boom")
        return _outcome("/out/good.opus")

    reports = acquire_many(queries, Path("/out"), downloader=dl, prober=lambda p: _info())
    assert [r.query for r in reports] == ["good", "bad"]
    assert reports[0].ok is True
    assert reports[1].ok is False
