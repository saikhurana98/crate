"""Tier-2 Telegram music-bot adapter.

The bot is queried by sending a message and collecting the audio it replies
with. The Telegram client is injected (request/download callables), so response
parsing, candidate selection (title guard + prefer-lossless), and the download
orchestration are unit tested without a live account or bot.
"""

from pathlib import Path

import pytest

from lbmd.adapters.sources.telegram_bot import (
    TgAudio,
    is_lossless,
    parse_audio_candidates,
    select_audio,
    telegram_download,
)


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
