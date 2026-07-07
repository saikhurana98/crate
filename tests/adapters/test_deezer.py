"""Tier-2 Deezer adapter — 16-bit FLAC via ARL + Blowfish decrypt.

The HTTP flow (auth/search/token/get_url/download) is injected; the pure pieces
— Blowfish key derivation, the CBC-stripe decryption, search parsing, and track
selection — are tested directly. The decrypt test round-trips through a real
Blowfish cipher so it verifies the exact stripe scheme Deezer uses.
"""

from pathlib import Path

import pytest
from Crypto.Cipher import Blowfish

from crate.adapters.sources.deezer import (
    DeezerTrack,
    blowfish_key,
    decrypt_stream,
    decrypt_track,
    deezer_download,
    parse_search,
    select_track,
)

SEARCH_RESPONSE = {
    "data": [
        {
            "id": 3135556,
            "title": "Harder, Better, Faster, Stronger",
            "artist": {"name": "Daft Punk"},
            "album": {"title": "Discovery"},
            "duration": 224,
        },
        {
            "id": 999,
            "title": "Something Else",
            "artist": {"name": "Daft Punk"},
            "album": {"title": "X"},
            "duration": 200,
        },
    ]
}


def test_blowfish_key_is_deterministic_16_bytes() -> None:
    k1 = blowfish_key(3135556)
    k2 = blowfish_key(3135556)
    assert k1 == k2
    assert len(k1) == 16
    assert blowfish_key(999) != k1  # id-specific


def _stripe_encrypt(plain: bytes, key: bytes) -> bytes:
    """Encrypt like Deezer serves it: every 3rd full 2048-byte chunk is
    Blowfish-CBC encrypted (IV 00..07), the rest is clear."""
    iv = bytes(range(8))
    out = bytearray()
    for i in range(0, len(plain), 2048):
        chunk = plain[i : i + 2048]
        idx = i // 2048
        if idx % 3 == 0 and len(chunk) == 2048:
            out += Blowfish.new(key, Blowfish.MODE_CBC, iv).encrypt(chunk)
        else:
            out += chunk
    return bytes(out)


def test_decrypt_track_roundtrips_the_stripe_scheme() -> None:
    key = blowfish_key(3135556)
    # 5 chunks: full, full, full, full, partial -> exercises encrypted (0,3) + clear + tail
    plain = bytes((i * 7) % 256 for i in range(2048 * 4 + 500))
    encrypted = _stripe_encrypt(plain, key)
    assert encrypted != plain
    assert decrypt_track(encrypted, key) == plain


def test_decrypt_stream_matches_whole_buffer_decrypt(tmp_path: Path) -> None:
    # The streamed file->file path must produce byte-identical output to the
    # in-memory whole-buffer decrypt for a multi-stripe payload (encrypted stripes
    # at index 0,3,6 + clear stripes between + a trailing partial chunk).
    key = blowfish_key(3135556)
    plain = bytes((i * 11) % 256 for i in range(2048 * 7 + 777))
    encrypted = _stripe_encrypt(plain, key)
    assert encrypted != plain

    src = tmp_path / "in.enc"
    dst = tmp_path / "out.flac"
    src.write_bytes(encrypted)
    decrypt_stream(src, dst, key)

    assert dst.read_bytes() == decrypt_track(encrypted, key) == plain


def test_parse_search_maps_tracks() -> None:
    tracks = parse_search(SEARCH_RESPONSE)
    assert len(tracks) == 2
    assert tracks[0].id == 3135556
    assert tracks[0].title == "Harder, Better, Faster, Stronger"
    assert tracks[0].artist == "Daft Punk"
    assert tracks[0].album == "Discovery"
    assert tracks[0].duration_s == 224


def test_select_track_title_guard() -> None:
    tracks = parse_search(SEARCH_RESPONSE)
    assert select_track(tracks, "Daft Punk Harder Better Faster Stronger").id == 3135556
    with pytest.raises(LookupError, match="no title-matching"):
        select_track(tracks, "Completely Different Song")


def _dz(id_: int, title: str, artist: str, duration_s: int, isrc: str | None) -> DeezerTrack:
    return DeezerTrack(id_, title, artist, "", duration_s, isrc)


def test_select_track_isrc_short_circuits() -> None:
    tracks = [
        _dz(1, "Levitating", "Cover Artist", 200, "GBWRONG00001"),  # wrong, first
        _dz(2, "Levitating", "Dua Lipa", 203, "GBAHT2000123"),  # wanted
    ]
    assert select_track(tracks, "anything", isrc="GBAHT2000123").id == 2


def test_select_track_prefilters_by_duration() -> None:
    tracks = [
        _dz(1, "Levitating", "Dua Lipa", 480, None),  # extended, off
        _dz(2, "Levitating", "Dua Lipa", 203, None),  # wanted
    ]
    assert select_track(tracks, "Dua Lipa Levitating", want_duration_s=203).id == 2


def test_select_track_artist_gate_drops_wrong_artist_same_title() -> None:
    # same-title track by a different artist with a coincidentally-matching duration
    # is gated out -> LookupError so the cascade advances.
    tracks = [_dz(1, "Hai Mera Dil Tu", "Alka Yagnik", 209, None)]
    with pytest.raises(LookupError, match="no title-matching"):
        select_track(tracks, "Alfaaz Hai Mera Dil", want_duration_s=210, want_artist="Alfaaz")


def test_select_track_artist_gate_keeps_correct_artist() -> None:
    tracks = [
        _dz(1, "Hai Mera Dil Tu", "Alka Yagnik", 209, None),
        _dz(2, "Hai Mera Dil", "Alfaaz", 210, None),
    ]
    got = select_track(tracks, "Alfaaz Hai Mera Dil", want_duration_s=210, want_artist="Alfaaz")
    assert got.id == 2


def test_deezer_download_resolves_and_decrypts(tmp_path: Path) -> None:
    key = blowfish_key(3135556)
    plain = b"fLaC" + bytes((i * 3) % 256 for i in range(2048 * 3))
    served = _stripe_encrypt(plain, key)
    calls: dict[str, object] = {}

    def search(q: str) -> object:
        return SEARCH_RESPONSE

    def resolve_url(track_id: int) -> str:
        calls["resolved"] = track_id
        return f"https://cdn.deezer/{track_id}.enc"

    def fetch(url: str, dest: Path) -> None:
        assert url.endswith("3135556.enc")
        assert dest.name.endswith(".enc.part")  # encrypted bytes stream to a sidecar
        dest.write_bytes(served)

    outcome = deezer_download(
        "Daft Punk Harder Better Faster Stronger",
        tmp_path,
        search=search,
        resolve_url=resolve_url,
        fetch=fetch,
        clock=iter([1.0, 6.0]).__next__,
    )
    assert outcome.tier == 2
    assert outcome.source == "deezer"
    assert outcome.elapsed_s == pytest.approx(5.0)
    assert outcome.path.suffix == ".flac"
    assert outcome.path.read_bytes() == plain  # decrypted correctly
    assert calls["resolved"] == 3135556
    assert not list(tmp_path.glob("*.part"))  # encrypted + decrypted temps cleaned up


def test_deezer_download_raises_when_no_match(tmp_path: Path) -> None:
    with pytest.raises(LookupError):
        deezer_download(
            "No Such Song Here",
            tmp_path,
            search=lambda q: SEARCH_RESPONSE,
            resolve_url=lambda i: "x",
            fetch=lambda u, d: None,
            clock=lambda: 0.0,
        )


def test_deezer_track_dataclass_immutable() -> None:
    t = DeezerTrack(1, "T", "A", "Al", 100, None)
    with pytest.raises(AttributeError):
        t.title = "x"  # type: ignore[misc]
