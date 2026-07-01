"""CLI smoke tests. The real `serve` command lands in a later unit; this pins
the app wiring, `version`, and the `download` report rendering (no network)."""

from pathlib import Path

from typer.testing import CliRunner

import lbmd.cli.main as cli
from lbmd import __version__
from lbmd.adapters.sources.ytdlp import DownloadOutcome
from lbmd.application.download import DownloadReport
from lbmd.cli.main import app
from lbmd.domain.audio import AudioInfo, QualityVerdict

runner = CliRunner()


def test_version_command_prints_version() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert __version__ in result.stdout


def test_help_lists_the_app() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "lbmd" in result.stdout.lower()


def test_download_renders_ok_and_fail_rows(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    ok = DownloadReport(
        query="Dua Lipa Levitating",
        outcome=DownloadOutcome(tmp_path / "Levitating.opus", 3, "ytdlp", 4.2, "q"),
        info=AudioInfo("opus", "webm", 48000, 2, None, 160000, 203.0, 4_100_000),
        verdict=QualityVerdict("lossy opus 160 kbps / 48 kHz", False),
        error=None,
    )
    bad = DownloadReport("missing track", None, None, None, "yt-dlp failed (1): not found")
    monkeypatch.setattr(cli, "acquire_many", lambda q, o: [ok, bad])

    result = runner.invoke(
        app, ["download", "Dua Lipa Levitating", "missing track", "-o", str(tmp_path)]
    )
    assert result.exit_code == 0
    assert "OK" in result.stdout and "Levitating.opus" in result.stdout
    assert "160 kbps" in result.stdout
    assert "FAIL" in result.stdout and "not found" in result.stdout
    assert "1/2 downloaded" in result.stdout
