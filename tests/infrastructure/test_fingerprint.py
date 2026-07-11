"""fpcalc -raw -json parsing (pure) + bounded-subprocess behaviour."""

import json
import subprocess
from pathlib import Path

import pytest

from crate.infrastructure.audio.fingerprint import (
    fingerprint,
    fpcalc_available,
    parse_fpcalc,
)

RAW_JSON = {"duration": 201, "fingerprint": [1, 2, 3, 4]}


def _completed(stdout: str) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr="")


def test_parse_fpcalc_returns_duration_and_raw_array() -> None:
    duration, fp = parse_fpcalc(RAW_JSON)
    assert duration == 201
    assert fp == [1, 2, 3, 4]


def test_parse_fpcalc_rejects_missing_array() -> None:
    with pytest.raises(ValueError, match="fingerprint array"):
        parse_fpcalc({"duration": 10})


def test_fingerprint_runs_fpcalc_and_parses() -> None:
    seen: list[list[str]] = []

    def runner(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        seen.append(cmd)
        return _completed(json.dumps(RAW_JSON))

    duration, fp = fingerprint(Path("/x/song.flac"), runner=runner)
    assert (duration, fp) == (201, [1, 2, 3, 4])
    assert seen[0][0] == "fpcalc"
    assert "-raw" in seen[0] and "/x/song.flac" in seen[0]


def test_fingerprint_translates_timeout() -> None:
    def runner(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd, 30)

    with pytest.raises(RuntimeError, match="timed out"):
        fingerprint(Path("/x/song.flac"), runner=runner)


def test_fpcalc_available_true_and_false() -> None:
    assert fpcalc_available(runner=lambda cmd: _completed("fpcalc version 1.5.1"))

    def missing(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError("fpcalc")

    assert not fpcalc_available(runner=missing)
