"""CENC decryption for Amazon Music streams (mp4decrypt + ffmpeg).

Verified end-to-end against a live monochrome.tf resolution: mp4decrypt
(Bento4) decrypts the fragmented MP4 with ``KID:key``, then ffmpeg remuxes the
FLAC-in-MP4 to a clean native FLAC. Requires ``mp4decrypt`` and ``ffmpeg`` on PATH.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path

# mp4decrypt + the ffmpeg remux are cheap on a healthy fragment but can wedge on
# a malformed one; 120s bounds each so decryption can never pin a worker thread.
_DECRYPT_TIMEOUT_S = 120

Runner = Callable[[list[str]], "subprocess.CompletedProcess[bytes]"]


def _default_runner(cmd: list[str]) -> subprocess.CompletedProcess[bytes]:  # pragma: no cover
    return subprocess.run(cmd, check=True, capture_output=True, timeout=_DECRYPT_TIMEOUT_S)


def mp4decrypt_cenc(
    enc: Path,
    kid_hex: str,
    key_hex: str,
    out: Path,
    *,
    runner: Runner = _default_runner,
) -> None:
    """Decrypt a CENC fragment to native FLAC.

    Each subprocess is bounded by a 120s timeout (``_default_runner``); a wedged
    mp4decrypt/ffmpeg surfaces as ``RuntimeError`` instead of pinning a worker
    thread. The runner is injected so the timeout translation is unit-tested.
    """
    dec = enc.with_suffix(".dec.mp4")
    try:
        runner(["mp4decrypt", "--key", f"{kid_hex}:{key_hex}", str(enc), str(dec)])
        runner(
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
            ]
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("mp4decrypt/ffmpeg timed out after 120s") from exc
    finally:
        dec.unlink(missing_ok=True)
