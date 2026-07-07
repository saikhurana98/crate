"""Tier-2 Telegram music-bot adapter.

The bot is queried by sending a message and collecting the audio it replies
with. The Telegram client is injected (request/download callables), so response
parsing, candidate selection (title guard + prefer-lossless), and the download
orchestration are unit tested without a live account or bot.
"""

from pathlib import Path

import pytest

from crate.adapters.sources.telegram_bot import (
    TgAudio,
    is_lossless,
    parse_audio_candidates,
    select_audio,
    telegram_download,
)
from crate.domain.identity import TrackIdentity


def _msg(name: str, mime: str, size: int, dur: int, bitrate: int, ref: str) -> dict:  # type: ignore[type-arg]
    return {
        "audio": {
            "file_name": name,
            "mime_type": mime,
            "file_size": size,
            "duration": dur,
            "bitrate": bitrate,
        },
        "ref": ref,
    }


MESSAGES = [
    {"text": "Here are your results:"},  # non-audio, ignored
    _msg("Marjaani - Pritam.mp3", "audio/mpeg", 8_000_000, 280, 320000, "m1"),
    _msg("Marjaani - Pritam.flac", "audio/flac", 71_000_000, 280, 1411000, "m2"),
    _msg("Channa Mereya - Pritam.flac", "audio/flac", 60_000_000, 270, 1411000, "m3"),
]


def test_parse_audio_candidates_extracts_only_audio() -> None:
    cands = parse_audio_candidates(MESSAGES)
    assert len(cands) == 3
    assert cands[0].filename == "Marjaani - Pritam.mp3"
    assert cands[0].ref == "m1"
    assert cands[1].bitrate_bps == 1411000


def test_is_lossless_by_extension_and_mime() -> None:
    assert is_lossless(TgAudio("x.flac", "audio/flac", 1, 1, 1, "r")) is True
    assert is_lossless(TgAudio("x.mp3", "audio/mpeg", 1, 1, 1, "r")) is False
    assert is_lossless(TgAudio("x.unknown", "audio/x-flac", 1, 1, 1, "r")) is True


def test_select_prefers_flac_over_mp3_for_same_title() -> None:
    chosen = select_audio(parse_audio_candidates(MESSAGES), "Marjaani Pritam")
    assert chosen.filename == "Marjaani - Pritam.flac"  # lossless wins over the 320 mp3


def test_select_title_guard_rejects_wrong_song() -> None:
    # only the wrong-song flac is present -> no title match -> raise
    wrong = [_msg("Channa Mereya - Pritam.flac", "audio/flac", 1, 1, 1411000, "m3")]
    with pytest.raises(LookupError, match="no title-matching"):
        select_audio(parse_audio_candidates(wrong), "Marjaani Pritam")


def test_select_raises_when_no_audio() -> None:
    with pytest.raises(LookupError):
        select_audio([], "anything")


def test_select_prefers_correct_artist_with_want_tokens() -> None:
    # two same-title FLACs by different artists (Artist - Title filenames); the
    # wanted artist wins on score and the unrelated ElCano is dropped by the gate.
    msgs = [
        _msg("ElCano - Mammoth.flac", "audio/flac", 1, 200, 1411000, "w"),
        _msg("Dimitri Vegas - Mammoth.flac", "audio/flac", 1, 200, 1411000, "r"),
    ]
    want = TrackIdentity(artist="Dimitri Vegas", title="Mammoth", duration_s=200)
    chosen = select_audio(parse_audio_candidates(msgs), "Dimitri Vegas Mammoth", want=want)
    assert chosen.ref == "r"


def test_select_audio_artist_gate_empties_pool_raises() -> None:
    # Alfaaz -> Alka regression: only title+duration match is by the wrong artist
    # (filename "Alka Yagnik - Hai Mera Dil Tu"); the gate drops it and the source
    # fails so the cascade advances.
    msgs = [_msg("Alka Yagnik - Hai Mera Dil Tu.flac", "audio/flac", 1, 209, 1411000, "w")]
    want = TrackIdentity(artist="Alfaaz", title="Hai Mera Dil", duration_s=210)
    with pytest.raises(LookupError, match="no title-matching"):
        select_audio(parse_audio_candidates(msgs), "Alfaaz Hai Mera Dil", want=want)


def test_select_audio_no_separator_uses_full_stem_as_artist() -> None:
    # no " - " separator -> whole stem is the candidate artist; it contains the
    # wanted token so the gate passes.
    msgs = [_msg("Alfaaz Hai Mera Dil.flac", "audio/flac", 1, 210, 1411000, "r")]
    want = TrackIdentity(artist="Alfaaz", title="Hai Mera Dil", duration_s=210)
    chosen = select_audio(parse_audio_candidates(msgs), "Alfaaz Hai Mera Dil", want=want)
    assert chosen.ref == "r"


def test_select_prefilters_by_duration() -> None:
    msgs = [
        _msg("Blinding Lights.flac", "audio/flac", 1, 480, 1411000, "long"),  # off-duration
        _msg("Blinding Lights.mp3", "audio/mpeg", 1, 200, 320000, "right"),  # near-duration
    ]
    want = TrackIdentity(title="Blinding Lights", duration_s=200)
    chosen = select_audio(parse_audio_candidates(msgs), "Blinding Lights", want=want)
    assert chosen.ref == "right"


def test_download_picks_and_writes(tmp_path: Path) -> None:
    downloaded: list[tuple[str, str]] = []

    def request(query: str) -> list:  # type: ignore[type-arg]
        assert query == "Marjaani Pritam"
        return MESSAGES

    def download(ref: object, dest: Path) -> None:
        downloaded.append((str(ref), dest.name))
        dest.write_bytes(b"fLaC\x00tg-bytes")

    outcome = telegram_download(
        "Marjaani Pritam",
        tmp_path,
        request=request,
        download=download,
        clock=iter([10.0, 15.0]).__next__,
    )
    assert outcome.tier == 2
    assert outcome.source == "telegram"
    assert outcome.elapsed_s == pytest.approx(5.0)
    assert outcome.path.suffix == ".flac"
    assert outcome.path.read_bytes().startswith(b"fLaC")
    assert downloaded[0][0] == "m2"  # the flac candidate's ref


def test_download_raises_when_no_match(tmp_path: Path) -> None:
    with pytest.raises(LookupError):
        telegram_download(
            "Marjaani Pritam",
            tmp_path,
            request=lambda q: [{"text": "no results"}],
            download=lambda r, d: None,
            clock=lambda: 0.0,
        )
