"""Chromaprint ``fpcalc`` adapter: read a file's raw acoustic fingerprint.

``parse_fpcalc`` is pure (takes fpcalc's parsed ``-json`` output) so it is fully
unit tested; ``fingerprint`` is the thin bounded-subprocess wrapper, and
``fpcalc_available`` probes the binary once so the whole review-fingerprint
feature can stay silently off when it is absent (ADR-0019). No new Python
dependency — this shells out exactly like the ffprobe adapter next door.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

# fpcalc on a full track returns in ~1 s; 30 s is a generous ceiling so a wedged
# process (corrupt/truncated file) can never pin a queue-worker thread forever.
_FPCALC_TIMEOUT_S = 30
# Cap analysis to the first 120 s — plenty to identify a recording, and bounds
# the work on long files.
_FPCALC_LENGTH_S = 120

Runner = Callable[[list[str]], "subprocess.CompletedProcess[str]"]


def parse_fpcalc(data: dict[str, Any]) -> tuple[int, list[int]]:
    """Turn ``fpcalc -raw -json`` output into ``(duration_s, raw_fingerprint)``.

    ``-raw`` makes the ``fingerprint`` field an array of 32-bit sub-fingerprints
    (rather than the compressed base64 string), which is what the pure
    :func:`crate.domain.fingerprint.similarity` compares.
    """
    fp = data.get("fingerprint")
    if not isinstance(fp, list):
        raise ValueError("fpcalc -raw did not return a fingerprint array")
    return int(data.get("duration", 0)), [int(x) for x in fp]


def _default_runner(cmd: list[str]) -> subprocess.CompletedProcess[str]:  # pragma: no cover
    return subprocess.run(
        cmd, capture_output=True, text=True, check=True, timeout=_FPCALC_TIMEOUT_S
    )


def fpcalc_available(*, runner: Runner = _default_runner) -> bool:
    """True when the ``fpcalc`` binary is present and runnable.

    Probed once at startup; when false the review fingerprint guard stays off and
    the tab degrades to metadata-only (the core Docker image may omit
    ``libchromaprint-tools``; the full image adds it)."""
    try:
        runner(["fpcalc", "-version"])
    except (OSError, subprocess.SubprocessError):
        return False
    return True


def fingerprint(path: Path, *, runner: Runner = _default_runner) -> tuple[int, list[int]]:
    """Fingerprint ``path`` → ``(duration_s, raw_fingerprint)``.

    Bounded by a 30 s timeout (``_default_runner``) so a wedged fpcalc can never
    permanently kill a queue-worker thread. The subprocess is injected so parsing
    and the timeout translation are unit-tested without spawning a process."""
    cmd = ["fpcalc", "-raw", "-length", str(_FPCALC_LENGTH_S), "-json", str(path)]
    try:
        result = runner(cmd)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("fpcalc timed out after 30s") from exc
    return parse_fpcalc(json.loads(result.stdout))
