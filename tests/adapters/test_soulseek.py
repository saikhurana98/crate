"""Tier-2 Soulseek adapter (via slskd REST).

The slskd client is injected (search -> responses, download -> local path), so
response flattening, candidate selection (title guard + prefer-lossless + free
slot + bitrate), and orchestration are unit tested without a live daemon.
"""

from pathlib import Path

import pytest

from crate.adapters.sources.soulseek import (
    SlskFile,
    format_rank,
    is_lossless,
    meets_bitrate_floor,
    parse_responses,
    rank_files,
    search_variants,
    select_file,
    soulseek_download,
)
from crate.domain.identity import TrackIdentity

# slskd GET /api/v0/searches/{id}/responses shape
RESPONSES = [
    {
        "username": "peerA",
        "uploadSpeed": 500,
        "hasFreeUploadSlot": False,
        "files": [
            {
                "filename": "@@x\\Music\\Marjaani - Pritam.mp3",
                "size": 8_000_000,
                "bitRate": 320,
                "length": 280,
            },
        ],
    },
    {
        "username": "peerB",
        "uploadSpeed": 2000,
        "hasFreeUploadSlot": True,
        "files": [
            {
                "filename": "D:\\flac\\Marjaani - Pritam.flac",
                "size": 71_000_000,
                "bitRate": 1411,
                "bitDepth": 24,
                "sampleRate": 48000,
                "length": 280,
            },
            {
                "filename": "D:\\flac\\Channa Mereya - Pritam.flac",
                "size": 60_000_000,
                "bitRate": 1411,
                "length": 270,
            },
        ],
    },
]


def test_parse_responses_flattens_users_and_files() -> None:
    cands = parse_responses(RESPONSES)
    assert len(cands) == 3
    assert cands[0].username == "peerA"
    assert cands[0].bitrate_bps == 320_000  # kbps -> bps
    assert cands[1].free_slot is True
    assert cands[1].filename.endswith("Marjaani - Pritam.flac")


def test_is_lossless_by_extension() -> None:
    assert is_lossless(SlskFile("u", "a\\x.flac", 1, 1411000, 24, 280, 48000, True, 1)) is True
    assert is_lossless(SlskFile("u", "a\\x.mp3", 1, 320000, None, 280, None, True, 1)) is False


def test_select_prefers_lossless_over_mp3_for_same_title() -> None:
    chosen = select_file(parse_responses(RESPONSES), "Marjaani Pritam")
    assert chosen.filename.endswith("Marjaani - Pritam.flac")  # flac beats the 320 mp3
    assert chosen.username == "peerB"


def test_select_title_guard_rejects_wrong_song() -> None:
    wrong = [
        {
            "username": "p",
            "hasFreeUploadSlot": True,
            "uploadSpeed": 1,
            "files": [{"filename": "x\\Channa Mereya.flac", "size": 1, "bitRate": 1411}],
        }
    ]
    with pytest.raises(LookupError, match="no title-matching"):
        select_file(parse_responses(wrong), "Marjaani Pritam")


def _slsk(
    name: str,
    *,
    bitrate_bps: int = 0,
    size: int = 1,
    depth: int | None = None,
    rate: int | None = None,
    length: int | None = 218,
    free: bool = True,
    speed: int = 1,
) -> SlskFile:
    return SlskFile("p", f"x\\{name}", size, bitrate_bps, depth, length, rate, free, speed)


def test_bitrate_floor_never_drops_lossless() -> None:
    # THE REGRESSION: slskd reports bitRate 0 for FLAC (bitrate is a lossy
    # attribute), so a floor applied to everything deleted almost every lossless
    # file on the network — the exact files the floor exists to reach.
    flac = _slsk("Temperature.flac", bitrate_bps=0, depth=16, rate=44100)
    assert meets_bitrate_floor(flac, 256_000) is True
    # the floor still does its job on lossy files
    assert meets_bitrate_floor(_slsk("Temperature.mp3", bitrate_bps=128_000), 256_000) is False
    assert meets_bitrate_floor(_slsk("Temperature.mp3", bitrate_bps=320_000), 256_000) is True


def test_rank_keeps_zero_bitrate_flac_above_the_floor() -> None:
    files = [
        _slsk("Sean Paul - Temperature.mp3", bitrate_bps=320_000),
        _slsk("Sean Paul - Temperature.flac", bitrate_bps=0, depth=16, rate=44100),
    ]
    ranked = rank_files(files, "Sean Paul Temperature", min_bitrate_kbps=256)
    assert len(ranked) == 2  # the flac survived the floor
    assert ranked[0].filename.endswith(".flac")


def test_rank_prefers_flac_despite_a_tidier_mp3_filename() -> None:
    # match_score penalises the filename's extra tokens, so an untidy-but-lossless
    # file used to lose to a cleanly-named MP3 of the same song. Ranking on query
    # coverage instead is blind to decoration, so format decides.
    files = [
        _slsk("Sean Paul - Temperature.mp3", bitrate_bps=320_000),
        _slsk(
            "01. Sean Paul - Temperature (Album Version) [2005].flac",
            bitrate_bps=0,
            depth=16,
            rate=44100,
        ),
    ]
    ranked = rank_files(files, "Sean Paul Temperature")
    assert ranked[0].filename.endswith(".flac")


def test_rank_still_rejects_a_different_song_by_the_same_artist() -> None:
    # preferring lossless must not let a different song ride in on its format:
    # the title guard drops it before ranking ever sees it
    files = [
        _slsk("Sean Paul - Temperature.mp3", bitrate_bps=320_000),
        _slsk("Sean Paul - Get Busy.flac", bitrate_bps=0, depth=16, rate=44100),
    ]
    ranked = rank_files(files, "Sean Paul Temperature")
    assert [f.filename for f in ranked] == ["x\\Sean Paul - Temperature.mp3"]


def test_rank_prefers_tagged_lossless_over_raw_pcm() -> None:
    # WAV is the same audio but untagged and ~3x the size; FLAC wins even though
    # the WAV is the larger file.
    files = [
        _slsk("Sean Paul - Temperature.wav", size=36_000_000, depth=16, rate=44100),
        _slsk("Sean Paul - Temperature.flac", size=23_000_000, depth=16, rate=44100),
    ]
    assert rank_files(files, "Sean Paul Temperature")[0].filename.endswith(".flac")
    assert format_rank(files[1]) > format_rank(files[0])


def test_rank_prefers_higher_bit_depth_then_bigger_file() -> None:
    hi = _slsk("Sean Paul - Temperature.flac", size=40_000_000, depth=24, rate=96000)
    lo = _slsk("Sean Paul - Temperature.flac", size=23_000_000, depth=16, rate=44100)
    assert rank_files([lo, hi], "Sean Paul Temperature")[0] is hi
    # same depth/rate -> the bigger file is the better rip
    big = _slsk("Sean Paul - Temperature.flac", size=30_000_000, depth=16, rate=44100)
    assert rank_files([lo, big], "Sean Paul Temperature")[0] is big


def test_select_honours_min_bitrate() -> None:
    only_low = [
        {
            "username": "p",
            "hasFreeUploadSlot": True,
            "uploadSpeed": 1,
            "files": [{"filename": "x\\Marjaani.mp3", "size": 1, "bitRate": 128}],
        }
    ]
    with pytest.raises(LookupError):
        select_file(parse_responses(only_low), "Marjaani Pritam", min_bitrate_kbps=256)


def test_rank_prefers_correct_artist_with_want_tokens() -> None:
    # two same-title FLACs by different artists (Artist - Title filenames); the
    # wanted artist wins on match_score even though both pass the title guard.
    # ElCano shares an artist token with neither the query nor "Dimitri Vegas", so
    # it is also dropped by the artist gate before scoring.
    responses = [
        {
            "username": "p",
            "hasFreeUploadSlot": True,
            "uploadSpeed": 1,
            "files": [
                {"filename": "x\\ElCano - Mammoth.flac", "size": 1, "bitRate": 1411, "length": 200},
                {
                    "filename": "x\\Dimitri Vegas - Mammoth.flac",
                    "size": 1,
                    "bitRate": 1411,
                    "length": 200,
                },
            ],
        }
    ]
    want = TrackIdentity(artist="Dimitri Vegas", title="Mammoth", duration_s=200)
    chosen = select_file(parse_responses(responses), "Dimitri Vegas Mammoth", want=want)
    assert chosen.filename.endswith("Dimitri Vegas - Mammoth.flac")


def test_select_file_artist_gate_empties_pool_raises() -> None:
    # Alfaaz -> Alka regression: the only title+duration match is by the wrong
    # artist (filename "Alka Yagnik - Hai Mera Dil Tu"); the artist gate drops it
    # so the source fails and the cascade advances.
    responses = [
        {
            "username": "p",
            "hasFreeUploadSlot": True,
            "uploadSpeed": 1,
            "files": [
                {
                    "filename": "x\\Alka Yagnik - Hai Mera Dil Tu.flac",
                    "size": 1,
                    "bitRate": 1411,
                    "length": 209,
                }
            ],
        }
    ]
    want = TrackIdentity(artist="Alfaaz", title="Hai Mera Dil", duration_s=210)
    with pytest.raises(LookupError, match="no title-matching"):
        select_file(parse_responses(responses), "Alfaaz Hai Mera Dil", want=want)


def test_select_file_no_separator_uses_full_stem_as_artist() -> None:
    # filename with no " - " separator -> whole stem is the candidate artist; here
    # the stem contains the wanted artist token so the gate passes.
    responses = [
        {
            "username": "p",
            "hasFreeUploadSlot": True,
            "uploadSpeed": 1,
            "files": [
                {
                    "filename": "x\\Alfaaz Hai Mera Dil.flac",
                    "size": 1,
                    "bitRate": 1411,
                    "length": 210,
                }
            ],
        }
    ]
    want = TrackIdentity(artist="Alfaaz", title="Hai Mera Dil", duration_s=210)
    chosen = select_file(parse_responses(responses), "Alfaaz Hai Mera Dil", want=want)
    assert chosen.filename.endswith("Alfaaz Hai Mera Dil.flac")


def test_rank_prefilters_by_duration_when_want_known() -> None:
    responses = [
        {
            "username": "p",
            "hasFreeUploadSlot": True,
            "uploadSpeed": 1,
            "files": [
                {"filename": "x\\Blinding Lights.flac", "size": 1, "bitRate": 1411, "length": 480},
                {"filename": "x\\Blinding Lights.mp3", "size": 1, "bitRate": 320, "length": 200},
            ],
        }
    ]
    # the FLAC would normally win, but it's off-duration (480s) and dropped; the
    # near-duration mp3 survives.
    want = TrackIdentity(title="Blinding Lights", duration_s=200)
    chosen = select_file(parse_responses(responses), "Blinding Lights", want=want)
    assert chosen.filename.endswith("Blinding Lights.mp3")


def test_download_selects_and_returns_local_path(tmp_path: Path) -> None:
    enqueued: list[tuple[str, str]] = []

    def search(query: str) -> list:  # type: ignore[type-arg]
        return RESPONSES

    def download(file: SlskFile, out_dir: Path) -> Path:
        enqueued.append((file.username, file.filename))
        dest = out_dir / "Marjaani - Pritam.flac"
        dest.write_bytes(b"fLaC\x00slsk")
        return dest

    outcome = soulseek_download(
        "Marjaani Pritam",
        tmp_path,
        search=search,
        download=download,
        clock=iter([1.0, 9.0]).__next__,
    )
    assert outcome.tier == 2
    assert outcome.source == "soulseek"
    assert outcome.elapsed_s == pytest.approx(8.0)
    assert outcome.path.suffix == ".flac"
    assert enqueued[0][0] == "peerB"  # chose the free-slot flac peer


def test_download_raises_when_no_match(tmp_path: Path) -> None:
    with pytest.raises(LookupError):
        soulseek_download(
            "Marjaani Pritam",
            tmp_path,
            search=lambda q: [],
            download=lambda f, d: Path("x"),
            clock=lambda: 0.0,
        )


def test_rank_files_orders_lossless_and_free_slot_first() -> None:
    ranked = rank_files(parse_responses(RESPONSES), "Marjaani Pritam")
    assert ranked[0].filename.endswith("Marjaani - Pritam.flac")  # flac + free slot on top
    assert len(ranked) == 2  # the wrong-song flac is filtered out by the title guard


def test_download_falls_to_next_peer_when_first_fails(tmp_path: Path) -> None:
    tried: list[str] = []

    def download(file: SlskFile, out_dir: Path) -> Path:
        tried.append(file.username)
        if file.username == "peerB":  # best peer stalls
            raise RuntimeError("peer stalled")
        dest = out_dir / "Marjaani - Pritam.mp3"
        dest.write_bytes(b"ok")
        return dest

    outcome = soulseek_download(
        "Marjaani Pritam",
        tmp_path,
        search=lambda q: RESPONSES,
        download=download,
        clock=iter([1.0, 4.0]).__next__,
    )
    assert outcome.source == "soulseek"
    assert tried == ["peerB", "peerA"]  # best peer failed -> fell to next


def test_search_variants_drops_stopwords_and_edge_tokens() -> None:
    # 'the'/'weeknd' block Soulseek's filename match; a title-only variant is tried
    variants = search_variants("The Weeknd Blinding Lights")
    assert variants[0] == "weeknd blinding lights"  # cleaned full (stopword dropped)
    assert "blinding lights" in variants  # drop leading token -> title only


def test_download_retries_reduced_query_when_full_query_empty(tmp_path: Path) -> None:
    seen: list[str] = []
    flac = {
        "username": "p",
        "hasFreeUploadSlot": True,
        "uploadSpeed": 1,
        "files": [{"filename": "x\\Blinding Lights.flac", "size": 1, "bitRate": 1411}],
    }

    def search(q: str) -> list:  # type: ignore[type-arg]
        seen.append(q)
        return [flac] if q == "blinding lights" else []  # only the reduced query hits

    outcome = soulseek_download(
        "The Weeknd Blinding Lights",
        tmp_path,
        search=search,
        download=lambda f, d: d / "Blinding Lights.flac",
        clock=iter([0.0, 1.0]).__next__,
    )
    assert outcome.source == "soulseek"
    assert "weeknd blinding lights" in seen and "blinding lights" in seen  # retried
