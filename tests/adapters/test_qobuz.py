"""Tier-1 Qobuz adapter — hi-res FLAC, direct download (no decryption).

The signed getFileUrl request is the only tricky bit; it's pure and tested
against the exact concatenation Qobuz expects. HTTP is injected, so search
parsing, selection, signing, and orchestration are tested without account/keys.
"""

import hashlib
from pathlib import Path

import pytest

from crate.adapters.sources.qobuz import (
    QobuzTrack,
    parse_search,
    qobuz_download,
    select_track,
    sign_get_file_url,
)

SEARCH_RESPONSE = {
    "tracks": {
        "items": [
            {
                "id": 12345,
                "title": "Get Lucky",
                "performer": {"name": "Daft Punk"},
                "album": {"title": "Random Access Memories"},
                "duration": 369,
                "maximum_bit_depth": 24,
                "maximum_sampling_rate": 88.2,
                "isrc": "USQX91300108",
            },
            {
                "id": 999,
                "title": "Instant Crush",
                "performer": {"name": "Daft Punk"},
                "album": {"title": "Random Access Memories"},
                "duration": 337,
                "maximum_bit_depth": 16,
                "maximum_sampling_rate": 44.1,
            },
        ]
    }
}


def test_sign_get_file_url_matches_qobuz_concatenation() -> None:
    # trackgetFileUrl + params in alpha order (format_id,intent,track_id) + ts + secret
    expected = hashlib.md5(
        b"trackgetFileUrlformat_id27intentstreamtrack_id1231700000000s3cr3t"
    ).hexdigest()
    assert sign_get_file_url(123, 27, 1_700_000_000, "s3cr3t") == expected


def test_parse_search_maps_hires_metadata() -> None:
    tracks = parse_search(SEARCH_RESPONSE)
    assert len(tracks) == 2
    t = tracks[0]
    assert t.id == 12345
    assert t.title == "Get Lucky"
    assert t.artist == "Daft Punk"
    assert t.album == "Random Access Memories"
    assert t.bit_depth == 24
    assert t.sample_rate_khz == 88.2
    assert t.isrc == "USQX91300108"


def test_select_track_prefers_isrc_then_title() -> None:
    tracks = parse_search(SEARCH_RESPONSE)
    assert select_track(tracks, "anything", isrc="USQX91300108").id == 12345
    assert select_track(tracks, "Daft Punk Instant Crush").id == 999
    with pytest.raises(LookupError, match="no title-matching"):
        select_track(tracks, "Totally Unrelated Track")


def test_qobuz_download_writes_flac_tier1(tmp_path: Path) -> None:
    calls: dict[str, object] = {}

    def search(q: str) -> object:
        return SEARCH_RESPONSE

    def get_file_url(track_id: int) -> str:
        calls["id"] = track_id
        return "https://cdn.qobuz/track.flac"

    def get_bytes(url: str) -> bytes:
        assert url.endswith(".flac")
        return b"fLaC\x00realqobuzflac"  # already FLAC, no decryption

    outcome = qobuz_download(
        "Daft Punk Get Lucky",
        tmp_path,
        search=search,
        get_file_url=get_file_url,
        get_bytes=get_bytes,
        clock=iter([2.0, 7.0]).__next__,
    )
    assert outcome.tier == 1
    assert outcome.source == "qobuz"
    assert outcome.elapsed_s == pytest.approx(5.0)
    assert outcome.path.suffix == ".flac"
    assert outcome.path.read_bytes().startswith(b"fLaC")
    assert calls["id"] == 12345


def test_qobuz_download_raises_when_no_match(tmp_path: Path) -> None:
    with pytest.raises(LookupError):
        qobuz_download(
            "No Match Here",
            tmp_path,
            search=lambda q: SEARCH_RESPONSE,
            get_file_url=lambda i: "x",
            get_bytes=lambda u: b"",
            clock=lambda: 0.0,
        )


def test_qobuz_track_immutable() -> None:
    t = QobuzTrack(1, "T", "A", "Al", 100, 24, 96.0, None)
    with pytest.raises(AttributeError):
        t.title = "x"  # type: ignore[misc]
