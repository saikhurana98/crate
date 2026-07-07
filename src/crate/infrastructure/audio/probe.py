"""ffprobe adapter: read the technical facts of an audio file.

`parse_ffprobe` is pure (takes ffprobe's parsed JSON) so it is fully unit
tested; `probe` is the thin subprocess wrapper around it.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

from crate.domain.audio import AudioInfo

# ffprobe should return in well under a second; 30s is a generous ceiling that
# still guarantees a wedged process (corrupt/truncated file) can never pin a
# queue-worker thread forever.
_PROBE_TIMEOUT_S = 30

Runner = Callable[[list[str]], "subprocess.CompletedProcess[str]"]


def _audio_stream(data: dict[str, Any]) -> dict[str, Any]:
    for stream in data.get("streams", []):
        if stream.get("codec_type") == "audio":
            return cast(dict[str, Any], stream)
    raise ValueError("no audio stream in ffprobe output")


def _int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def parse_ffprobe(data: dict[str, Any], size_bytes: int) -> AudioInfo:
    """Turn ffprobe's ``-show_format -show_streams`` JSON into an AudioInfo."""
    stream = _audio_stream(data)
    fmt = data.get("format", {})

    bit_depth = _int_or_none(stream.get("bits_per_raw_sample")) or _int_or_none(
        stream.get("bits_per_sample")
    )
    # bits_per_sample is 0 for lossy codecs; treat 0 as "unknown".
    if bit_depth == 0:
        bit_depth = None

    bitrate = _int_or_none(stream.get("bit_rate")) or _int_or_none(fmt.get("bit_rate"))

    return AudioInfo(
        codec=str(stream.get("codec_name", "unknown")),
        container=str(fmt.get("format_name", "unknown")),
        sample_rate_hz=_int_or_none(stream.get("sample_rate")) or 0,
        channels=_int_or_none(stream.get("channels")) or 0,
        bit_depth=bit_depth,
        bitrate_bps=bitrate,
        duration_s=float(fmt.get("duration", 0.0) or 0.0),
        size_bytes=size_bytes,
    )


def _default_runner(cmd: list[str]) -> subprocess.CompletedProcess[str]:  # pragma: no cover
    return subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=_PROBE_TIMEOUT_S)


def probe(path: Path, *, runner: Runner = _default_runner) -> AudioInfo:
    """Run ffprobe on ``path`` and return an AudioInfo.

    Bounded by a 30s timeout (``_default_runner``) so a wedged ffprobe can never
    permanently kill a queue-worker thread. The subprocess is injected so the
    timeout translation is unit-tested without spawning a process.
    """
    cmd = [
        "ffprobe",
        "-v",
        "quiet",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    try:
        result = runner(cmd)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("ffprobe timed out after 30s") from exc
    return parse_ffprobe(json.loads(result.stdout), size_bytes=path.stat().st_size)
