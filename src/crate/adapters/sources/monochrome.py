"""Monochrome Tracks API adapter: fast search and direct FLAC by track ID."""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from crate.domain.candidate import Candidate
from crate.domain.download import DownloadOutcome
from crate.domain.identity import TrackIdentity
from crate.domain.matching import artist_matches, duration_prefilter, match_score, title_matches

Search = Callable[[str], dict[str, Any]]
Fetch = Callable[[str, Path], None]
Clock = Callable[[], float]
_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_MILLISECONDS_PER_SECOND = 1000


@dataclass(frozen=True, slots=True)
class MonoTrack:
    id: str
    title: str
    artist: str
    album: str | None
    duration_s: int | None
    isrc: str | None
    cover_url: str | None


def parse_search(data: dict[str, Any]) -> list[MonoTrack]:
    """Normalize the Tracks API search response, ignoring unplayable rows."""
    tracks: list[MonoTrack] = []
    for item in data.get("tracks", []):
        if not isinstance(item, dict) or item.get("playable") is False:
            continue
        track_id = str(item.get("trackId") or item.get("id") or "")
        if not track_id.isdecimal():
            continue
        names = item.get("artistNames") or []
        if not names:
            names = [a.get("name") for a in item.get("artists", []) if isinstance(a, dict)]
        artist = ", ".join(str(name) for name in names if name) or "Unknown Artist"
        duration_ms = item.get("duration")
        duration_s = (
            round(
                float(duration_ms) / _MILLISECONDS_PER_SECOND
                if float(duration_ms) > _MILLISECONDS_PER_SECOND
                else float(duration_ms)
            )
            if duration_ms is not None
            else None
        )
        tracks.append(
            MonoTrack(
                id=track_id,
                title=str(item.get("title") or "Unknown Title"),
                artist=artist,
                album=item.get("albumTitle") or item.get("releaseTitle") or None,
                duration_s=duration_s,
                isrc=item.get("isrc") or None,
                cover_url=item.get("artwork") or None,
            )
        )
    return tracks


def select_track(tracks: list[MonoTrack], query: str, want: TrackIdentity | None) -> MonoTrack:
    """Prefer exact ISRC; otherwise gate title and artist, then rank matches."""
    if want and want.isrc:
        for track in tracks:
            if track.isrc and track.isrc.upper() == want.isrc.upper():
                return track
    title_query = want.title if want and want.title else query
    matches = [track for track in tracks if title_matches(track.title, title_query)]
    if want and want.artist:
        matches = [track for track in matches if artist_matches(track.artist, want.artist)]
    if not matches:
        raise LookupError("no title/artist-matching Monochrome track")
    pool = duration_prefilter(matches, want.duration_s if want else None, lambda t: t.duration_s)
    return max(pool, key=lambda t: match_score(t.title, t.artist, query))


def _fetch_track(track_id: str, artist: str, title: str, out_dir: Path, fetch: Fetch) -> Path:
    """Fetch to a sidecar, check FLAC magic, then publish the finished file."""
    stem = _UNSAFE.sub("_", f"{artist} - {title} [{track_id}]")
    dest = out_dir / f"{stem}.flac"
    part = out_dir / f"{stem}.flac.part"
    try:
        fetch(track_id, part)
        with part.open("rb") as fh:
            if fh.read(4) != b"fLaC":
                raise ValueError("Monochrome returned a non-FLAC file")
        part.replace(dest)
    finally:
        part.unlink(missing_ok=True)
    return dest


def monochrome_download(
    query: str,
    out_dir: Path,
    want: TrackIdentity | None = None,
    *,
    search: Search,
    fetch: Fetch,
    clock: Clock = time.monotonic,
) -> DownloadOutcome:
    start = clock()
    track = select_track(parse_search(search(query)), query, want)
    path = _fetch_track(track.id, track.artist, track.title, out_dir, fetch)
    return DownloadOutcome(
        path=path,
        tier=1,
        source="monochrome",
        elapsed_s=clock() - start,
        query=query,
        title=track.title,
        artist=track.artist,
        album=track.album,
        art_url=track.cover_url,
    )


def search_candidates(
    query: str,
    want: TrackIdentity | None = None,
    *,
    search: Search,
) -> list[Candidate]:
    """Expose search rows for Review; refs download the exact track without re-search."""
    title_query = want.title if want and want.title else query
    return [
        Candidate(
            source="monochrome",
            tier=1,
            ref=track.id,
            title=track.title,
            artist=track.artist,
            album=track.album,
            quality_label="FLAC",
            lossless=True,
            duration_s=track.duration_s,
            size_bytes=None,
            art_url=track.cover_url,
        )
        for track in parse_search(search(query))
        if title_matches(track.title, title_query)
        and (not want or not want.artist or artist_matches(track.artist, want.artist))
    ]


def download_candidate(
    candidate: Candidate,
    out_dir: Path,
    want: TrackIdentity | None = None,
    *,
    fetch: Fetch,
    clock: Clock = time.monotonic,
) -> DownloadOutcome:
    """Download the candidate's stored track ID directly, without another search."""
    start = clock()
    path = _fetch_track(candidate.ref, candidate.artist, candidate.title, out_dir, fetch)
    return DownloadOutcome(
        path=path,
        tier=1,
        source="monochrome",
        elapsed_s=clock() - start,
        query=f"{candidate.artist} - {candidate.title}",
        title=candidate.title,
        artist=candidate.artist,
        album=candidate.album,
        art_url=candidate.art_url,
    )
