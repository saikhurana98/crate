"""Audio file quality model + classification (ADR-0005, domain side).

Pure. Given the technical facts of a decoded stream, decide whether it is
lossless and produce a human-readable quality label. No I/O here.
"""

from __future__ import annotations

from dataclasses import dataclass

LOSSLESS_CODECS = frozenset({"flac", "alac", "wav", "aiff"})


@dataclass(frozen=True, slots=True)
class AudioInfo:
    """Technical facts about one audio file, as read by a probe."""

    codec: str
    container: str
    sample_rate_hz: int
    channels: int
    bit_depth: int | None
    bitrate_bps: int | None
    duration_s: float
    size_bytes: int


@dataclass(frozen=True, slots=True)
class QualityVerdict:
    label: str
    lossless: bool


def classify(info: AudioInfo) -> QualityVerdict:
    """Map an :class:`AudioInfo` to a quality verdict."""
    khz = info.sample_rate_hz / 1000
    if info.codec in LOSSLESS_CODECS:
        depth = info.bit_depth if info.bit_depth is not None else 16
        return QualityVerdict(
            label=f"lossless {depth}-bit / {khz:g} kHz {info.codec}", lossless=True
        )
    if info.bitrate_bps is not None:
        kbps = round(info.bitrate_bps / 1000)
        label = f"lossy {info.codec} {kbps} kbps / {khz:g} kHz"
    else:
        label = f"lossy {info.codec} / {khz:g} kHz"
    return QualityVerdict(label=label, lossless=False)
