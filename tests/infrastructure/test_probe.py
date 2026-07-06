"""ffprobe JSON -> AudioInfo parsing (pure, no subprocess)."""

import pytest

from crate.infrastructure.audio.probe import parse_ffprobe

OPUS_JSON = {
    "streams": [
        {"codec_type": "video", "codec_name": "mjpeg"},
        {
            "codec_type": "audio",
            "codec_name": "opus",
            "sample_rate": "48000",
            "channels": 2,
            "bit_rate": "160000",
        },
    ],
    "format": {"format_name": "webm", "duration": "201.5", "bit_rate": "162000"},
}

FLAC_JSON = {
    "streams": [
        {
            "codec_type": "audio",
            "codec_name": "flac",
            "sample_rate": "96000",
            "channels": 2,
            "bits_per_raw_sample": "24",
        }
    ],
    "format": {"format_name": "flac", "duration": "180.0"},
}


def test_parse_opus_stream() -> None:
    info = parse_ffprobe(OPUS_JSON, size_bytes=4_100_000)
    assert info.codec == "opus"
    assert info.sample_rate_hz == 48000
    assert info.channels == 2
    assert info.bitrate_bps == 160000
    assert info.bit_depth is None
    assert info.duration_s == pytest.approx(201.5)
    assert info.size_bytes == 4_100_000


def test_parse_flac_reads_bit_depth() -> None:
    info = parse_ffprobe(FLAC_JSON, size_bytes=9_000_000)
    assert info.codec == "flac"
    assert info.bit_depth == 24
    assert info.sample_rate_hz == 96000


def test_parse_falls_back_to_format_bitrate_when_stream_missing_it() -> None:
    data = {
        "streams": [
            {"codec_type": "audio", "codec_name": "aac", "sample_rate": "44100", "channels": 2}
        ],
        "format": {"format_name": "mov,mp4,m4a", "duration": "150", "bit_rate": "256000"},
    }
    info = parse_ffprobe(data, size_bytes=5_000_000)
    assert info.bitrate_bps == 256000


def test_parse_raises_when_no_audio_stream() -> None:
    with pytest.raises(ValueError, match="no audio stream"):
        parse_ffprobe({"streams": [{"codec_type": "video"}], "format": {}}, size_bytes=1)
