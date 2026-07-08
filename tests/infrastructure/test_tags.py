"""embed_cover round-trip via mutagen. Builds a tiny real FLAC with ffmpeg
(skipped when ffmpeg is unavailable) and asserts a front-cover picture reads
back — the one branch of the otherwise ``# pragma: no cover`` mutagen wrapper we
exercise for real."""

import shutil
import subprocess
from pathlib import Path

import pytest

from crate.infrastructure.audio.tags import embed_cover

_FFMPEG = shutil.which("ffmpeg")

# A 1x1 JPEG (valid enough for mutagen to store as picture data).
_JPEG = (
    bytes.fromhex(
        "ffd8ffe000104a46494600010100000100010000ffdb004300"
        "080606070605080707070909080a0c140d0c0b0b0c1912130f14"
        "1d1a1f1e1d1a1c1c20242e2720222c231c1c28372"
        "9"
    )
    + b"\x00" * 8
    + bytes.fromhex("ffd9")
)


def _tiny_flac(path: Path) -> None:
    subprocess.run(
        [
            _FFMPEG,  # type: ignore[list-item]
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=44100:cl=mono",
            "-t",
            "0.2",
            "-y",
            str(path),
        ],
        check=True,
        capture_output=True,
    )


@pytest.mark.skipif(_FFMPEG is None, reason="ffmpeg not installed")
def test_embed_cover_flac_roundtrip(tmp_path: Path) -> None:
    from mutagen.flac import FLAC

    flac = tmp_path / "t.flac"
    _tiny_flac(flac)
    assert not FLAC(flac).pictures  # no art to start

    embed_cover(flac, _JPEG)

    pics = FLAC(flac).pictures
    assert len(pics) == 1
    assert pics[0].type == 3  # front cover
    assert pics[0].mime == "image/jpeg"
    assert pics[0].data == _JPEG


@pytest.mark.skipif(_FFMPEG is None, reason="ffmpeg not installed")
def test_embed_cover_flac_replaces_existing(tmp_path: Path) -> None:
    from mutagen.flac import FLAC

    flac = tmp_path / "t.flac"
    _tiny_flac(flac)
    embed_cover(flac, _JPEG)
    embed_cover(flac, _JPEG + b"second")  # clears then re-adds -> exactly one

    pics = FLAC(flac).pictures
    assert len(pics) == 1
    assert pics[0].data == _JPEG + b"second"


def test_embed_cover_unknown_container_is_noop(tmp_path: Path) -> None:
    # mutagen.File returns None for a non-audio file -> silently do nothing.
    junk = tmp_path / "note.txt"
    junk.write_bytes(b"not audio")
    embed_cover(junk, _JPEG)  # must not raise
    assert junk.read_bytes() == b"not audio"
