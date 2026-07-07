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

from crate.domain.download import DownloadOutcome
from crate.domain.identity import TrackIdentity
from crate.domain.matching import duration_prefilter, match_score, title_matches

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


def select_audio(
    candidates: list[TgAudio], query: str, want: TrackIdentity | None = None
) -> TgAudio:
    """Keep title-matching candidates, then rank best-first.

    When ``want`` is known the pool is duration-prefiltered and the ranking leads
    with ``match_score`` of the filename against the query + artist tokens (so a
    same-title file by the wrong artist loses), then prefers lossless, higher
    bitrate, larger. Raises LookupError when nothing matches, so the cascade
    advances a tier.
    """
    matching = [c for c in candidates if title_matches(Path(c.filename).stem, query)]
    if want is not None:
        matching = duration_prefilter(matching, want.duration_s, lambda c: c.duration_s)
    if not matching:
        raise LookupError("no title-matching Telegram audio")
    artist = want.artist if want and want.artist else ""
    score_query = f"{query} {artist}".strip()

    def score(c: TgAudio) -> float:
        return match_score(Path(c.filename).stem, artist, score_query)

    return max(matching, key=lambda c: (score(c), is_lossless(c), c.bitrate_bps, c.size_bytes))


def telegram_download(
    query: str,
    out_dir: Path,
    want: TrackIdentity | None = None,
    *,
    request: Request,
    download: Download,
    clock: Clock = time.monotonic,
) -> DownloadOutcome:
    """Query the bot, pick the best matching audio, download it.

    ``want`` (when known) feeds the duration prefilter + artist-aware ranking."""
    start = clock()
    chosen = select_audio(parse_audio_candidates(request(query)), query, want)
    dest = out_dir / _UNSAFE.sub("_", chosen.filename)
    download(chosen.ref, dest)
    return DownloadOutcome(
        path=dest, tier=2, source="telegram", elapsed_s=clock() - start, query=query
    )
