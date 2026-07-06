"""CENC decryption for Amazon Music streams (mp4decrypt + ffmpeg).

Verified end-to-end against a live monochrome.tf resolution: mp4decrypt
(Bento4) decrypts the fragmented MP4 with ``KID:key``, then ffmpeg remuxes the
FLAC-in-MP4 to a clean native FLAC. Requires ``mp4decrypt`` and ``ffmpeg`` on PATH.
"""

from __future__ import annotations

import subprocess
from pathlib import Path


def mp4decrypt_cenc(enc: Path, kid_hex: str, key_hex: str, out: Path) -> None:  # pragma: no cover
    dec = enc.with_suffix(".dec.mp4")
    try:
        subprocess.run(
            ["mp4decrypt", "--key", f"{kid_hex}:{key_hex}", str(enc), str(dec)],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-v",
                "error",
                "-i",
                str(dec),
                "-map",
                "0:a",
                "-c:a",
                "flac",
                str(out),
            ],
            check=True,
            capture_output=True,
        )
    finally:
        dec.unlink(missing_ok=True)
