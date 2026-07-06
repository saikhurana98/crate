"""Tier-3 yt-dlp adapter. Pure command-building + result parsing are tested
with an injected fake runner/clock; no network in unit tests."""

import subprocess
from pathlib import Path

import pytest

from crate.adapters.sources.ytdlp import build_command, download, extract_path


def test_build_command_uses_ytsearch_for_free_text() -> None:
    cmd = build_command("Dua Lipa Levitating", Path("/tmp/out"))
    assert cmd[0] == "yt-dlp"
    assert "bestaudio" in cmd
    assert cmd[-1] == "ytsearch1:Dua Lipa Levitating"


def test_build_command_passes_url_through() -> None:
    url = "https://youtube.com/watch?v=abc"
    cmd = build_command(url, Path("/tmp/out"))
    assert cmd[-1] == url


def test_extract_path_takes_last_nonempty_line() -> None:
    stdout = "some log\n\n/tmp/out/Song [id].opus\n"
    assert extract_path(stdout) == Path("/tmp/out/Song [id].opus")


def test_download_returns_outcome_with_elapsed() -> None:
    calls: list[list[str]] = []
    times = iter([100.0, 103.5])

    def fake_runner(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="/tmp/out/Track [x].opus\n", stderr="")

    outcome = download(
        "Karan Aujla Boyfriend",
        Path("/tmp/out"),
        runner=fake_runner,
        clock=lambda: next(times),
    )
    assert outcome.path == Path("/tmp/out/Track [x].opus")
    assert outcome.elapsed_s == pytest.approx(3.5)
    assert outcome.tier == 3
    assert outcome.source == "ytdlp"
    assert calls  # runner was invoked


def test_download_raises_on_nonzero_exit() -> None:
    def failing_runner(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="boom")

    with pytest.raises(RuntimeError, match="yt-dlp failed"):
        download("nope", Path("/tmp/out"), runner=failing_runner, clock=lambda: 0.0)
