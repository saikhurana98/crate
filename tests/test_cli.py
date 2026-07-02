"""CLI smoke tests. The real `serve` command lands in a later unit; this pins
the app wiring, `version`, and the `download` report rendering (no network)."""

from pathlib import Path

from typer.testing import CliRunner

import lbmd.cli.main as cli
from lbmd import __version__
from lbmd.application.cascade import CascadeAttempt, CascadeResult
from lbmd.application.download import DownloadReport
from lbmd.cli.main import app
from lbmd.domain.audio import AudioInfo, QualityVerdict
from lbmd.domain.download import DownloadOutcome

runner = CliRunner()


def test_version_command_prints_version() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert __version__ in result.stdout


def test_help_lists_the_app() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "lbmd" in result.stdout.lower()


def test_download_renders_cascade_trail_and_winner(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    won = DownloadReport(
        query="Dua Lipa Levitating",
        outcome=DownloadOutcome(tmp_path / "Levitating.opus", 3, "ytdlp", 4.2, "q"),
        info=AudioInfo("opus", "webm", 48000, 2, None, 160000, 203.0, 4_100_000),
        verdict=QualityVerdict("lossy opus 160 kbps / 48 kHz", False),
        error=None,
    )
    good = CascadeResult(
        "Dua Lipa Levitating",
        won,
        [
            CascadeAttempt("monochrome", 1, False, "Turnstile JWT required"),
            CascadeAttempt("ytdlp", 3, True, "lossy opus 160 kbps / 48 kHz"),
        ],
    )
    bad = CascadeResult(
        "missing", None, [CascadeAttempt("ytdlp", 3, False, "yt-dlp failed (1): not found")]
    )
    results = iter([good, bad])
    monkeypatch.setattr(cli, "build_tiers", lambda: [])
    monkeypatch.setattr(cli, "run_cascade", lambda q, o, t, prober: next(results))

    result = runner.invoke(app, ["download", "Dua Lipa Levitating", "missing", "-o", str(tmp_path)])
    assert result.exit_code == 0
    assert "OK" in result.stdout and "Levitating.opus" in result.stdout
    assert "T1:monochrome✗ -> T3:ytdlp✓" in result.stdout  # cascade trail, Tier 2 skipped
    assert "won tier 3" in result.stdout
    assert "FAIL" in result.stdout and "not found" in result.stdout
    assert "1/2 acquired" in result.stdout
