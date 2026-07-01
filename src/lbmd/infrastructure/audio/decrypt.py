"""CENC decryption wrapper (ffmpeg).

NOTE: Amazon Music streams use CENC (AES-CTR, subsample). Stock ffmpeg
``-decryption_key`` only partially decodes Amazon's scheme in testing; a robust
path uses the Monochrome instance's ``/api/decrypt-stream`` endpoint or a custom
CENC decryptor. This wrapper is the ffmpeg attempt and is intentionally thin.
"""

from __future__ import annotations

import subprocess
from pathlib import Path


def ffmpeg_cenc_decrypt(enc: Path, key_hex: str, out: Path) -> None:  # pragma: no cover
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-decryption_key",
            key_hex,
            "-i",
            str(enc),
            "-c:a",
            "flac",
            str(out),
        ],
        check=True,
    )
