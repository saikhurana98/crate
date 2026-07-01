"""Tier-1 Monochrome adapter — Amazon Music path -> 24-bit FLAC (ADR-0015).

Reverse-engineered from a real capture (HAR) of a working monochrome.tf
download:
  metadata: GET {tidal_base}/search/?s=<q>       -> {"data": {"items": [...]}}
  stream:   GET {amz_base}/api/track/?track=&duration=&album=&artist=&quality=UHD
            (X-Turnstile-JWT header) -> {stream_url, decryption_key, available_qualities}
  file:     GET <cloudfront stream_url>           -> CENC-encrypted fMP4 (FLAC)
  decrypt:  injected (ffmpeg / instance decrypt-stream) -> native FLAC

HTTP, clock, and the decrypt step are injected, so parsing + selection + the
orchestration are unit tested without network, tokens, or crypto.
"""

from pathlib import Path

import pytest

from lbmd.adapters.sources.monochrome import (
    build_amz_params,
    monochrome_download,
    parse_amz_stream,
    parse_search,
    pick_best,
)

SEARCH_RESPONSE = {
    "data": {
        "items": [
            {
                "id": 508267072,
                "title": "Dealer",
                "duration": 110,
                "audioQuality": "HI_RES_LOSSLESS",
                "mediaMetadata": {"tags": ["HIRES_LOSSLESS"]},
                "artist": {"name": "Diljit Dosanjh"},
                "album": {"title": "Dealer"},
            }
        ]
    }
}

AMZ_RESPONSE = {
    "asin": "B0GRPZM1Q6",
    "stream_url": "https://cf.example/enc-uhd.mp4?ql=UHD_48",
    "decryption_key": "2748e836153cb6a42fc9c40a7e17ee36",
    "quality_selected": "UHD_48",
    "available_qualities": [
        {
            "quality": "UHD_48",
            "codec": "flac",
            "bitDepth": 24,
            "sampleRate": 48000,
            "streamUrl": "https://cf.example/enc-uhd.mp4?ql=UHD_48",
        },
        {
            "quality": "HD_44",
            "codec": "flac",
            "bitDepth": 16,
            "sampleRate": 44100,
            "streamUrl": "https://cf.example/enc-hd.mp4?ql=HD_44",
        },
        {
            "quality": "SD_HIGH",
            "codec": "opus",
            "bitDepth": None,
            "sampleRate": 48000,
            "streamUrl": "https://cf.example/sd.mp4",
        },
    ],
}


def test_parse_search_maps_metadata() -> None:
    tracks = parse_search(SEARCH_RESPONSE)
    assert len(tracks) == 1
    t = tracks[0]
    assert t.id == 508267072
    assert t.title == "Dealer"
    assert t.artist == "Diljit Dosanjh"
    assert t.album == "Dealer"
    assert t.duration_s == 110
    assert t.hires is True


def test_pick_best_raises_on_empty() -> None:
    with pytest.raises(LookupError, match="no Monochrome match"):
        pick_best([])


def test_build_amz_params_from_track() -> None:
    track = pick_best(parse_search(SEARCH_RESPONSE))
    params = build_amz_params(track)
    assert params["track"] == "Dealer"
    assert params["album"] == "Dealer"
    assert params["artist"] == "Diljit Dosanjh"
    assert params["duration"] == 110
    assert params["quality"] == "UHD"


def test_parse_amz_selects_highest_bitdepth_flac() -> None:
    stream = parse_amz_stream(AMZ_RESPONSE)
    assert stream.codec == "flac"
    assert stream.bit_depth == 24
    assert stream.sample_rate == 48000
    assert stream.decryption_key == "2748e836153cb6a42fc9c40a7e17ee36"
    assert stream.stream_url.endswith("ql=UHD_48")


def test_parse_amz_raises_when_no_flac() -> None:
    resp = {
        "decryption_key": "k",
        "available_qualities": [
            {
                "quality": "SD",
                "codec": "opus",
                "bitDepth": None,
                "sampleRate": 48000,
                "streamUrl": "u",
            }
        ],
    }
    with pytest.raises(RuntimeError, match="no FLAC quality"):
        parse_amz_stream(resp)


def _get_json(url: str, params: dict[str, object], headers: dict[str, str] | None) -> object:
    if "/search/" in url:
        return SEARCH_RESPONSE
    assert headers is not None and headers.get("X-Turnstile-JWT") == "jwt-123"
    return AMZ_RESPONSE


def test_download_produces_flac_via_injected_decrypt(tmp_path: Path) -> None:
    downloaded_enc: dict[str, bytes] = {}
    decrypt_calls: list[tuple[str, str]] = []

    def get_bytes(url: str) -> bytes:
        downloaded_enc["url"] = url.encode()
        return b"ENCRYPTED-CENC-MP4-BYTES"

    def fake_decrypt(enc: Path, key: str, out: Path) -> None:
        decrypt_calls.append((key, out.name))
        out.write_bytes(b"fLaC\x00decrypted-24bit-flac")

    outcome = monochrome_download(
        "Diljit Dealer",
        tmp_path,
        get_json=_get_json,
        get_bytes=get_bytes,
        decrypt=fake_decrypt,
        jwt="jwt-123",
        clock=iter([100.0, 106.0]).__next__,
    )
    assert outcome.tier == 1
    assert outcome.source == "monochrome"
    assert outcome.elapsed_s == pytest.approx(6.0)
    assert outcome.path.suffix == ".flac"
    assert outcome.path.read_bytes().startswith(b"fLaC")
    assert decrypt_calls and decrypt_calls[0][0] == "2748e836153cb6a42fc9c40a7e17ee36"
    # encrypted temp cleaned up
    assert not list(tmp_path.glob("*.mp4"))


def test_download_requires_jwt(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="Turnstile JWT required"):
        monochrome_download(
            "Diljit Dealer",
            tmp_path,
            get_json=_get_json,
            get_bytes=lambda u: b"",
            decrypt=lambda e, k, o: None,
            jwt="",
            clock=lambda: 0.0,
        )
