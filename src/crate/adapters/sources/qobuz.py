"""Tier-1 source adapter: Qobuz -> hi-res FLAC (direct, no decryption).

Qobuz serves plain FLAC once you have a signed getFileUrl — no DRM, no browser,
no crypto. The only subtlety is the request signature (md5 of the method + params
in alpha order + timestamp + app_secret), which is pure and tested here. HTTP is
injected; the client (infrastructure) holds the app id/secret + user token.
"""

from __future__ import annotations

import hashlib
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from crate.domain.download import DownloadOutcome
from crate.domain.matching import title_matches

Search = Callable[[str], Any]  # query -> Qobuz search JSON
GetFileUrl = Callable[[int], str]  # track id -> signed direct FLAC url
GetBytes = Callable[[str], bytes]
Clock = Callable[[], float]

# hi-res <=192kHz 24-bit; the client can fall back to 7 (<=96k) / 6 (CD FLAC).
FORMAT_HIRES = 27

_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


@dataclass(frozen=True, slots=True)
class QobuzTrack:
    id: int
    title: str
    artist: str
    album: str
    duration_s: int | None
    bit_depth: int | None
    sample_rate_khz: float | None
    isrc: str | None


def sign_get_file_url(track_id: int, format_id: int, request_ts: int, app_secret: str) -> str:
    """Qobuz request signature for track/getFileUrl.

    md5 of: method ('trackgetFileUrl') + params in alphabetical order with no
    separators (format_id, intent, track_id) + timestamp + app_secret.
    """
    payload = (
        f"trackgetFileUrlformat_id{format_id}intentstreamtrack_id{track_id}{request_ts}{app_secret}"
    )
    return hashlib.md5(payload.encode()).hexdigest()


def parse_search(data: dict[str, Any]) -> list[QobuzTrack]:
    """Parse a Qobuz track/search response."""
    tracks: list[QobuzTrack] = []
    for item in data.get("tracks", {}).get("items", []):
        performer = item.get("performer") or {}
        tracks.append(
            QobuzTrack(
                id=int(item["id"]),
                title=str(item.get("title", "Unknown")),
                artist=str(performer.get("name", "Unknown")),
                album=str(item.get("album", {}).get("title", "")),
                duration_s=item.get("duration"),
                bit_depth=item.get("maximum_bit_depth"),
                sample_rate_khz=item.get("maximum_sampling_rate"),
                isrc=item.get("isrc"),
            )
        )
    return tracks


def select_track(tracks: list[QobuzTrack], query: str, isrc: str | None = None) -> QobuzTrack:
    """Prefer an exact ISRC match; else the first title-matching result."""
    if isrc:
        for track in tracks:
            if track.isrc and track.isrc.upper() == isrc.upper():
                return track
    for track in tracks:
        if title_matches(track.title, query):
            return track
    raise LookupError("no title-matching Qobuz track")


def qobuz_download(
    query: str,
    out_dir: Path,
    *,
    search: Search,
    get_file_url: GetFileUrl,
    get_bytes: GetBytes,
    isrc: str | None = None,
    clock: Clock = time.monotonic,
) -> DownloadOutcome:
    """Search Qobuz, resolve the signed FLAC url, download it (already FLAC)."""
    start = clock()
    track = select_track(parse_search(search(query)), query, isrc)
    content = get_bytes(get_file_url(track.id))

    name = _UNSAFE.sub("_", f"{track.artist} - {track.title} [{track.id}]") + ".flac"
    dest = out_dir / name
    dest.write_bytes(content)
    return DownloadOutcome(
        path=dest, tier=1, source="qobuz", elapsed_s=clock() - start, query=query
    )
