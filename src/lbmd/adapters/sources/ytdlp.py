"""Tier-3 source adapter: yt-dlp best-audio download (ADR-0015 Tier 3).

Lowest tier — lossy fallback (YouTube best audio, typically Opus ~160 kbps or
AAC). Emits an instant playable file when the FLAC tiers cannot deliver.

The subprocess is injected (``runner``) and the clock is injected so the
command-building and result-parsing logic is unit tested without network.
"""

from __future__ import annotations

import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

Runner = Callable[[list[str]], "subprocess.CompletedProcess[str]"]
Clock = Callable[[], float]


@dataclass(frozen=True, slots=True)
class DownloadOutcome:
    path: Path
    tier: int
    source: str
    elapsed_s: float
    query: str


def build_command(query: str, out_dir: Path) -> list[str]:
    """Build the yt-dlp argv. Free text becomes a ``ytsearch1:`` query; a URL
    is passed through unchanged."""
    target = query if query.startswith(("http://", "https://")) else f"ytsearch1:{query}"
    return [
        "yt-dlp",
        "--no-playlist",
        "--no-warnings",
        "-f",
        "bestaudio",
        "-o",
        f"{out_dir}/%(title)s [%(id)s].%(ext)s",
        "--print",
        "after_move:filepath",
        target,
    ]


def extract_path(stdout: str) -> Path:
    """The ``--print after_move:filepath`` output is the resolved file path."""
    lines = [ln.strip() for ln in stdout.splitlines() if ln.strip()]
    if not lines:
        raise RuntimeError("yt-dlp produced no output path")
    return Path(lines[-1])


def _default_runner(cmd: list[str]) -> subprocess.CompletedProcess[str]:  # pragma: no cover
    return subprocess.run(cmd, capture_output=True, text=True, check=False)


def download(
    query: str,
    out_dir: Path,
    *,
    runner: Runner = _default_runner,
    clock: Clock = time.monotonic,
) -> DownloadOutcome:
    """Download the best-audio rendition of ``query`` into ``out_dir``."""
    cmd = build_command(query, out_dir)
    start = clock()
    result = runner(cmd)
    elapsed = clock() - start
    if result.returncode != 0:
        raise RuntimeError(f"yt-dlp failed ({result.returncode}): {result.stderr.strip()}")
    return DownloadOutcome(
        path=extract_path(result.stdout),
        tier=3,
        source="ytdlp",
        elapsed_s=elapsed,
        query=query,
    )
