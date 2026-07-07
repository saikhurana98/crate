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
from crate.domain.identity import TrackIdentity

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


def _mono(id_: int, title: str, artist: str) -> QobuzTrack:
    return QobuzTrack(id_, title, artist, "", None, 24, 44.1, None)


def _dur(id_: int, title: str, artist: str, duration_s: int | None, isrc: str | None) -> QobuzTrack:
    return QobuzTrack(id_, title, artist, "", duration_s, 24, 44.1, isrc)


def test_select_track_isrc_short_circuits_two_same_title_candidates() -> None:
    # Two songs titled "Mammoth"; the WRONG one is first. Feeding the requested
    # track's ISRC must select the right one even though the title guard alone
    # would score both. This is the wrong-song regression the phase targets.
    tracks = [
        _dur(1, "Mammoth", "ElCano", 200, "GBWRONG00001"),  # wrong, listed first
        _dur(2, "Mammoth", "Dimitri Vegas & Like Mike, MOGUAI", 195, "BEA0Q1900001"),  # wanted
    ]
    got = select_track(tracks, "Dimitri Vegas Mammoth", isrc="BEA0Q1900001")
    assert got.id == 2


def test_select_track_isrc_wins_even_when_duration_off() -> None:
    # An exact ISRC is authoritative: a slightly-off search duration must not veto
    # it (ISRC short-circuits before the duration prefilter).
    tracks = [_dur(1, "Song", "A", 999, "USEXACT00001")]  # duration far from wanted
    got = select_track(tracks, "A Song", isrc="USEXACT00001", want_duration_s=200)
    assert got.id == 1


def test_select_track_prefilters_by_duration() -> None:
    # Two same-title/same-artist candidates; only the one near the wanted duration
    # survives the prefilter (e.g. a 3-min single vs an 8-min extended mix).
    tracks = [
        _dur(1, "Levitating", "Dua Lipa", 480, None),  # extended mix, off
        _dur(2, "Levitating", "Dua Lipa", 200, None),  # the wanted 3:20 cut
    ]
    got = select_track(tracks, "Dua Lipa Levitating", want_duration_s=202)
    assert got.id == 2


def test_select_track_duration_filter_kept_when_it_would_empty_pool() -> None:
    # If no candidate is near the wanted duration, keep the pool (a wrong duration
    # beats no result) — same rule Phase 0 gave yt-dlp.
    tracks = [_dur(1, "Levitating", "Dua Lipa", 480, None)]
    assert select_track(tracks, "Dua Lipa Levitating", want_duration_s=200).id == 1


def test_select_track_uses_artist_to_break_same_title_tie() -> None:
    # Regression: two songs titled "Mammoth" — the wrong artist appeared first.
    # Selection must pick the one whose ARTIST also matches the query.
    tracks = [
        _mono(1, "Mammoth", "ElCano"),  # unrelated same-title song, listed first
        _mono(2, "Mammoth", "Dimitri Vegas & Like Mike, MOGUAI"),  # the requested one
    ]
    got = select_track(tracks, "Dimitri Vegas, MOGUAI, Like Mike Mammoth")
    assert got.id == 2 and got.artist.startswith("Dimitri Vegas")


def test_select_track_artist_gate_drops_wrong_artist_same_title() -> None:
    # Only candidate is a same-title track by a different artist -> gate empties the
    # pool -> LookupError so the cascade advances (ISRC unknown here, as on the
    # sources that can't match by ISRC).
    tracks = [_dur(1, "Hai Mera Dil Tu", "Alka Yagnik", 209, None)]
    with pytest.raises(LookupError, match="no title-matching"):
        select_track(tracks, "Alfaaz Hai Mera Dil", want_duration_s=210, want_artist="Alfaaz")


def test_select_track_artist_gate_keeps_correct_artist() -> None:
    tracks = [
        _dur(1, "Hai Mera Dil Tu", "Alka Yagnik", 209, None),
        _dur(2, "Hai Mera Dil", "Alfaaz", 210, None),
    ]
    got = select_track(tracks, "Alfaaz Hai Mera Dil", want_duration_s=210, want_artist="Alfaaz")
    assert got.id == 2


def test_qobuz_download_writes_flac_tier1(tmp_path: Path) -> None:
    calls: dict[str, object] = {}

    def search(q: str) -> object:
        return SEARCH_RESPONSE

    def get_file_url(track_id: int) -> str:
        calls["id"] = track_id
        return "https://cdn.qobuz/track.flac"

    def fetch(url: str, dest: Path) -> None:
        assert url.endswith(".flac")
        assert dest.name.endswith(".part")  # streamed to a sidecar, renamed on success
        dest.write_bytes(b"fLaC\x00realqobuzflac")  # already FLAC, no decryption

    outcome = qobuz_download(
        "Daft Punk Get Lucky",
        tmp_path,
        search=search,
        get_file_url=get_file_url,
        fetch=fetch,
        clock=iter([2.0, 7.0]).__next__,
    )
    assert outcome.tier == 1
    assert outcome.source == "qobuz"
    assert outcome.elapsed_s == pytest.approx(5.0)
    assert outcome.path.suffix == ".flac"
    assert outcome.path.read_bytes().startswith(b"fLaC")
    assert calls["id"] == 12345
    assert not list(tmp_path.glob("*.part"))  # temp sidecar cleaned up


def test_qobuz_download_threads_want_isrc(tmp_path: Path) -> None:
    # SEARCH_RESPONSE has Get Lucky (isrc USQX91300108) and Instant Crush (no isrc).
    # Passing a `want` with the Get Lucky ISRC selects it regardless of the query.
    picked: dict[str, int] = {}

    def get_file_url(track_id: int) -> str:
        picked["id"] = track_id
        return "https://cdn.qobuz/track.flac"

    def fetch(url: str, dest: Path) -> None:
        dest.write_bytes(b"fLaC\x00x")

    outcome = qobuz_download(
        "totally unrelated words",
        tmp_path,
        TrackIdentity(artist="Daft Punk", title="Get Lucky", isrc="USQX91300108", duration_s=369),
        search=lambda q: SEARCH_RESPONSE,
        get_file_url=get_file_url,
        fetch=fetch,
        clock=iter([0.0, 1.0]).__next__,
    )
    assert picked["id"] == 12345 and outcome.source == "qobuz"


def test_qobuz_download_raises_when_no_match(tmp_path: Path) -> None:
    with pytest.raises(LookupError):
        qobuz_download(
            "No Match Here",
            tmp_path,
            search=lambda q: SEARCH_RESPONSE,
            get_file_url=lambda i: "x",
            fetch=lambda u, d: None,
            clock=lambda: 0.0,
        )


def test_qobuz_track_immutable() -> None:
    t = QobuzTrack(1, "T", "A", "Al", 100, 24, 96.0, None)
    with pytest.raises(AttributeError):
        t.title = "x"  # type: ignore[misc]
