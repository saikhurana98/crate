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
from crate.domain.matching import match_score, title_matches

Search = Callable[[str], Any]  # query -> Qobuz search JSON
GetFileUrl = Callable[[int], str]  # track id -> signed direct FLAC url
Fetch = Callable[[str, Path], None]  # (url, dest) -> stream the FLAC to dest
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
    """Prefer an exact ISRC match; else the best title+artist match.

    Among title-matching results, pick the one whose artist best matches the
    query too — so a requested song isn't swapped for an unrelated track that
    merely shares the title."""
    if isrc:
        for track in tracks:
            if track.isrc and track.isrc.upper() == isrc.upper():
                return track
    matching = [t for t in tracks if title_matches(t.title, query)]
    if not matching:
        raise LookupError("no title-matching Qobuz track")
    return max(matching, key=lambda t: match_score(t.title, t.artist, query))


def qobuz_download(
    query: str,
    out_dir: Path,
    *,
    search: Search,
    get_file_url: GetFileUrl,
    fetch: Fetch,
    isrc: str | None = None,
    clock: Clock = time.monotonic,
) -> DownloadOutcome:
    """Search Qobuz, resolve the signed FLAC url, stream it to disk (already FLAC).

    Streams to a ``.part`` sidecar and renames on success, so an interrupted
    download never leaves a half file that a later probe would treat as real."""
    start = clock()
    track = select_track(parse_search(search(query)), query, isrc)

    name = _UNSAFE.sub("_", f"{track.artist} - {track.title} [{track.id}]") + ".flac"
    dest = out_dir / name
    part = dest.with_name(dest.name + ".part")
    try:
        fetch(get_file_url(track.id), part)
        part.replace(dest)
    finally:
        part.unlink(missing_ok=True)
    return DownloadOutcome(
        path=dest,
        tier=1,
        source="qobuz",
        elapsed_s=clock() - start,
        query=query,
        title=track.title,
        artist=track.artist,
        album=track.album or None,
    )
