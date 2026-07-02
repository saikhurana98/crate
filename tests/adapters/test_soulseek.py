"""Tier-2 Soulseek adapter (via slskd REST).

The slskd client is injected (search -> responses, download -> local path), so
response flattening, candidate selection (title guard + prefer-lossless + free
slot + bitrate), and orchestration are unit tested without a live daemon.
"""

from pathlib import Path

import pytest

from lbmd.adapters.sources.soulseek import (
    SlskFile,
    is_lossless,
    parse_responses,
    select_file,
    soulseek_download,
)

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


def test_download_selects_and_returns_local_path(tmp_path: Path) -> None:
    enqueued: list[tuple[str, str]] = []

    def search(query: str) -> list:  # type: ignore[type-arg]
        assert query == "Marjaani Pritam"
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
