"""CENC decrypt orchestration — bounded-subprocess behaviour with a fake runner."""

import subprocess
from pathlib import Path

import pytest

from crate.infrastructure.audio.decrypt import mp4decrypt_cenc


def test_mp4decrypt_runs_both_stages_and_cleans_up(tmp_path: Path) -> None:
    enc = tmp_path / "frag.mp4"
    enc.write_bytes(b"enc")
    out = tmp_path / "out.flac"
    calls: list[list[str]] = []

    def fake_runner(cmd: list[str]) -> subprocess.CompletedProcess[bytes]:
        calls.append(cmd)
        # emulate mp4decrypt writing the intermediate .dec.mp4 the finally-block prunes
        if cmd[0] == "mp4decrypt":
            enc.with_suffix(".dec.mp4").write_bytes(b"dec")
        return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")

    mp4decrypt_cenc(enc, "kid", "key", out, runner=fake_runner)

    assert [c[0] for c in calls] == ["mp4decrypt", "ffmpeg"]
    assert "kid:key" in calls[0]
    assert not enc.with_suffix(".dec.mp4").exists()  # intermediate cleaned up


def test_mp4decrypt_raises_runtimeerror_on_timeout(tmp_path: Path) -> None:
    enc = tmp_path / "frag.mp4"
    enc.write_bytes(b"enc")
    out = tmp_path / "out.flac"

    def timing_out_runner(cmd: list[str]) -> subprocess.CompletedProcess[bytes]:
        raise subprocess.TimeoutExpired(cmd, 120)

    with pytest.raises(RuntimeError, match="timed out after 120s"):
        mp4decrypt_cenc(enc, "kid", "key", out, runner=timing_out_runner)
