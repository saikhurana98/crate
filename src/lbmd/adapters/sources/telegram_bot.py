"""Tier-2 source adapter: Telegram music bot.

Sends a query to a music-download bot and collects the audio it replies with,
then picks the best title-matching, prefer-lossless candidate and downloads it.
Telegram bots vary wildly in their conversation protocol (direct file reply vs
inline-button result menus) — this targets the common direct-reply pattern;
per-bot quirks are handled in the client (see infrastructure/telegram).

The Telegram client is injected as ``request`` (query -> reply messages) and
``download`` (message ref -> file), so all selection logic is pure + tested.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from lbmd.domain.download import DownloadOutcome
from lbmd.domain.matching import title_matches

# request(query) -> list of reply "messages" (dicts with an optional "audio" + "ref")
Request = Callable[[str], list[dict[str, Any]]]
Download = Callable[[Any, Path], None]  # (message ref, destination) -> writes the file
Clock = Callable[[], float]

_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_LOSSLESS_EXT = frozenset({".flac", ".alac", ".wav", ".aiff"})
_LOSSLESS_MIME = ("flac", "alac", "wav", "aiff", "x-flac")


@dataclass(frozen=True, slots=True)
class TgAudio:
    filename: str
    mime: str
    size_bytes: int
    duration_s: int
    bitrate_bps: int
    ref: Any  # opaque handle the client uses to download this message


def parse_audio_candidates(messages: list[dict[str, Any]]) -> list[TgAudio]:
    """Pull audio attachments out of a bot's reply messages."""
    out: list[TgAudio] = []
    for msg in messages:
        audio = msg.get("audio")
        if not audio:
            continue
        out.append(
            TgAudio(
                filename=str(audio.get("file_name", "audio")),
                mime=str(audio.get("mime_type", "")),
                size_bytes=int(audio.get("file_size", 0)),
                duration_s=int(audio.get("duration", 0)),
                bitrate_bps=int(audio.get("bitrate", 0)),
                ref=msg.get("ref"),
            )
        )
    return out


def is_lossless(audio: TgAudio) -> bool:
    ext = Path(audio.filename).suffix.lower()
    return ext in _LOSSLESS_EXT or any(tag in audio.mime.lower() for tag in _LOSSLESS_MIME)


def select_audio(candidates: list[TgAudio], query: str) -> TgAudio:
    """Keep title-matching candidates, then prefer lossless, higher bitrate, larger.

    Raises LookupError when nothing matches, so the cascade advances a tier.
    """
    matching = [c for c in candidates if title_matches(Path(c.filename).stem, query)]
    if not matching:
        raise LookupError("no title-matching Telegram audio")
    return max(matching, key=lambda c: (is_lossless(c), c.bitrate_bps, c.size_bytes))


def telegram_download(
    query: str,
    out_dir: Path,
    *,
    request: Request,
    download: Download,
    clock: Clock = time.monotonic,
) -> DownloadOutcome:
    """Query the bot, pick the best matching audio, download it."""
    start = clock()
    chosen = select_audio(parse_audio_candidates(request(query)), query)
    dest = out_dir / _UNSAFE.sub("_", chosen.filename)
    download(chosen.ref, dest)
    return DownloadOutcome(
        path=dest, tier=2, source="telegram", elapsed_s=clock() - start, query=query
    )
