"""Quality classification of a downloaded audio file (ADR-0005 domain side)."""

from crate.domain.audio import AudioInfo, classify


def _info(**kw: object) -> AudioInfo:
    base: dict[str, object] = {
        "codec": "opus",
        "container": "webm",
        "sample_rate_hz": 48000,
        "channels": 2,
        "bit_depth": None,
        "bitrate_bps": 160000,
        "duration_s": 200.0,
        "size_bytes": 4_000_000,
    }
    base.update(kw)
    return AudioInfo(**base)  # type: ignore[arg-type]


def test_lossy_opus_is_not_lossless() -> None:
    v = classify(_info(codec="opus", bitrate_bps=160000))
    assert v.lossless is False
    assert "lossy" in v.label
    assert "160" in v.label  # kbps surfaced


def test_flac_24bit_is_lossless_endgoal() -> None:
    v = classify(_info(codec="flac", container="flac", bit_depth=24, sample_rate_hz=96000))
    assert v.lossless is True
    assert "24-bit" in v.label
    assert "96" in v.label  # kHz surfaced


def test_flac_without_depth_defaults_to_16bit_cd() -> None:
    v = classify(_info(codec="flac", container="flac", bit_depth=None, sample_rate_hz=44100))
    assert v.lossless is True
    assert "16-bit" in v.label


def test_aac_m4a_lossy() -> None:
    v = classify(_info(codec="aac", container="m4a", bitrate_bps=256000))
    assert v.lossless is False
    assert "256" in v.label


def test_missing_bitrate_label_is_still_produced() -> None:
    v = classify(_info(codec="mp3", bitrate_bps=None))
    assert v.lossless is False
    assert "mp3" in v.label
