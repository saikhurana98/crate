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
    MonoTrack,
    build_amz_params,
    monochrome_download,
    parse_amz_stream,
    parse_kid,
    parse_search,
    pick_best,
    select,
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
                "isrc": "QT65Z2673203",
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
    assert t.isrc == "QT65Z2673203"


def test_pick_best_raises_on_empty() -> None:
    with pytest.raises(LookupError, match="no Monochrome match"):
        pick_best([])


def _t(id: int, hires: bool, isrc: str | None) -> MonoTrack:
    return MonoTrack(id, "T", "A", "Al", 100, hires, isrc)


def test_select_prefers_exact_isrc_match_over_hires() -> None:
    tracks = [_t(1, True, "AAA111111111"), _t(2, False, "BBB222222222")]
    # ISRC points at the non-hi-res track #2 -> exact master wins over hi-res heuristic
    assert select(tracks, "bbb222222222").id == 2  # case-insensitive


def test_select_falls_back_to_hires_when_no_isrc_match() -> None:
    tracks = [_t(1, False, "AAA111111111"), _t(2, True, "BBB222222222")]
    assert select(tracks, "ZZZ999999999").id == 2  # no match -> hi-res pick_best


def test_select_without_isrc_is_pick_best() -> None:
    tracks = [_t(1, False, None), _t(2, True, None)]
    assert select(tracks, None).id == 2


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


KID_HEX = "ac460c37460d7e197f5c9a37dfa55cd2"
# minimal fMP4 bytes carrying a tenc box: "tenc" + 8 bytes + 16-byte KID
ENC_MP4 = (
    b"\x00\x00\x00$ftypmp41\x00\x00\x00\x1ctenc" + bytes(8) + bytes.fromhex(KID_HEX) + b"\x00\x00"
)


def test_parse_kid_reads_default_kid_from_tenc() -> None:
    assert parse_kid(ENC_MP4) == KID_HEX


def test_parse_kid_raises_without_tenc() -> None:
    with pytest.raises(ValueError, match="not CENC-encrypted"):
        parse_kid(b"ftypmp41 no encryption here")


def test_download_resolves_downloads_and_decrypts(tmp_path: Path) -> None:
    amz_calls: list[dict[str, object]] = []
    decrypt_calls: list[tuple[str, str, str]] = []

    def search_json(query: str) -> object:
        assert query == "Diljit Dealer"
        return SEARCH_RESPONSE

    def amz_fetch(params: dict[str, object]) -> object:
        amz_calls.append(params)
        return AMZ_RESPONSE

    def get_bytes(url: str) -> bytes:
        assert url.endswith("ql=UHD_48")  # highest FLAC selected
        return ENC_MP4

    def fake_decrypt(enc: Path, kid: str, key: str, out: Path) -> None:
        decrypt_calls.append((kid, key, out.name))
        out.write_bytes(b"fLaC\x00decrypted-24bit-flac")

    outcome = monochrome_download(
        "Diljit Dealer",
        tmp_path,
        search_json=search_json,
        amz_fetch=amz_fetch,
        get_bytes=get_bytes,
        decrypt=fake_decrypt,
        clock=iter([100.0, 106.0]).__next__,
    )
    assert outcome.tier == 1
    assert outcome.source == "monochrome"
    assert outcome.elapsed_s == pytest.approx(6.0)
    assert outcome.path.suffix == ".flac"
    assert outcome.path.read_bytes().startswith(b"fLaC")
    assert amz_calls[0]["track"] == "Dealer"
    # decrypt got the KID parsed from the file + the key from the amz response
    assert decrypt_calls[0][0] == KID_HEX
    assert decrypt_calls[0][1] == "2748e836153cb6a42fc9c40a7e17ee36"
    assert not list(tmp_path.glob("*.mp4"))  # encrypted temp cleaned up
