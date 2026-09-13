"""Chromaprint decompression against golden fpcalc output (compressed vs -raw)."""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from crate.domain.chromaprint import decompress, pack_raw, to_signed32, unpack_raw
from crate.domain.fingerprint import similarity

GOLDEN = json.loads((Path(__file__).parent / "fixtures" / "chromaprint_golden.json").read_text())


@pytest.mark.parametrize("name", ["sweep20", "am9"])
def test_decompress_matches_fpcalc_raw(name: str) -> None:
    case = GOLDEN[name]
    fp = decompress(case["compressed"])
    assert fp.algorithm == 1
    # The decode is exactly the header's count. Those values match fpcalc's -raw
    # output. A separate -raw run can print one extra trailing sub-fingerprint
    # (seen with fpcalc 1.5.1), which is why this compares a prefix.
    assert fp.raw == case["raw"][: len(fp.raw)]
    assert len(case["raw"]) - len(fp.raw) in (0, 1)


def test_decoded_fingerprint_is_identical_to_raw_by_similarity() -> None:
    case = GOLDEN["sweep20"]
    assert similarity(decompress(case["compressed"]).raw, case["raw"]) == 1.0  # prefix-aligned


def test_pack_round_trip() -> None:
    raw = GOLDEN["sweep20"]["raw"]
    assert unpack_raw(pack_raw(raw)) == raw and len(pack_raw(raw)) == 4 * len(raw)


def test_to_signed32() -> None:
    assert to_signed32([0, 1, 2**31 - 1, 2**31, 2**32 - 1]) == [0, 1, 2**31 - 1, -(2**31), -1]


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


@pytest.mark.parametrize(
    "bad",
    [
        "",  # no header
        "AQA",  # header too short
        "!!!not-base64!!!",
        _b64(bytes([1, 0, 0, 5]) + b"\x00"),  # claims 5 sub-fingerprints, only a few terminators
        _b64(
            bytes([1, 0, 0, 1]) + bytes([0b00000111])
        ),  # needs an exceptional value that isn't there
    ],
)
def test_malformed_fingerprints_raise(bad: str) -> None:
    with pytest.raises(ValueError):
        decompress(bad)


def test_bit_positions_past_32_are_rejected() -> None:
    # gaps 7+31 (exceptional) -> bit 38: impossible for a 32-bit sub-fingerprint
    body = bytes([0b00000111]) + bytes([0b00011111])  # normal: 7, 0 ... ; exceptional: 31
    with pytest.raises(ValueError):
        decompress(_b64(bytes([1, 0, 0, 1]) + body))
