"""ffprobe adapter: read the technical facts of an audio file.

`parse_ffprobe` is pure (takes ffprobe's parsed JSON) so it is fully unit
tested; `probe` is the thin subprocess wrapper around it.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, cast

from lbmd.domain.audio import AudioInfo


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


def probe(path: Path) -> AudioInfo:  # pragma: no cover - thin subprocess wrapper
    """Run ffprobe on ``path`` and return an AudioInfo."""
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "quiet",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return parse_ffprobe(json.loads(result.stdout), size_bytes=path.stat().st_size)
