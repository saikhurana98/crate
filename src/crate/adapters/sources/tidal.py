"""TIDAL catalog adapter backed by Rhythmax/ODSkyler's public services."""

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


@dataclass(frozen=True, slots=True)
class TidalTrack:
    id: str
    title: str
    artist: str
    album: str | None
    duration_s: int | None
    isrc: str | None
    cover_url: str | None
    track_no: int | None
    disc_no: int | None
    quality: str


def _cover_url(cover: object) -> str | None:
    value = str(cover or "").strip()
    if not value:
        return None
    if value.startswith("https://"):
        return value
    return f"https://resources.tidal.com/images/{value.replace('-', '/')}/1280x1280.jpg"


def _integer(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def parse_search(data: dict[str, Any]) -> list[TidalTrack]:
    """Normalize TIDAL's ``search/tracks`` rows and preserve tagging metadata."""
    raw = data.get("items") or (data.get("tracks") or {}).get("items") or []
    tracks: list[TidalTrack] = []
    for item in raw:
        if not isinstance(item, dict) or item.get("allowStreaming") is False:
            continue
        track_id = str(item.get("id") or "")
        if not track_id.isdecimal():
            continue
        artists = item.get("artists") or []
        names = [a.get("name") for a in artists if isinstance(a, dict) and a.get("name")]
        fallback_artist = (item.get("artist") or {}).get("name")
        artist = ", ".join(str(name) for name in names) or str(fallback_artist or "Unknown Artist")
        album = item.get("album") or {}
        quality = str(item.get("audioQuality") or "LOSSLESS")
        tracks.append(
            TidalTrack(
                id=track_id,
                title=str(item.get("title") or "Unknown Title"),
                artist=artist,
                album=str(album.get("title")) if album.get("title") else None,
                duration_s=_integer(item.get("duration")),
                isrc=str(item.get("isrc")) if item.get("isrc") else None,
                cover_url=_cover_url(album.get("cover")),
                track_no=_integer(item.get("trackNumber")),
                disc_no=_integer(item.get("volumeNumber")),
                quality=quality,
            )
        )
    return tracks


def select_track(tracks: list[TidalTrack], query: str, want: TrackIdentity | None) -> TidalTrack:
    if want and want.isrc:
        for track in tracks:
            if track.isrc and track.isrc.upper() == want.isrc.upper():
                return track
    title_query = want.title if want and want.title else query
    matches = [track for track in tracks if title_matches(track.title, title_query)]
    if want and want.artist:
        matches = [track for track in matches if artist_matches(track.artist, want.artist)]
    if not matches:
        raise LookupError("no title/artist-matching TIDAL track")
    pool = duration_prefilter(matches, want.duration_s if want else None, lambda t: t.duration_s)
    return max(pool, key=lambda t: match_score(t.title, t.artist, query))


def _fetch_track(track: TidalTrack, out_dir: Path, fetch: Fetch) -> Path:
    stem = _UNSAFE.sub("_", f"{track.artist} - {track.title} [{track.id}]")
    dest = out_dir / f"{stem}.flac"
    part = out_dir / f"{stem}.flac.part"
    try:
        fetch(track.id, part)
        with part.open("rb") as fh:
            if fh.read(4) != b"fLaC":
                raise ValueError("TIDAL returned a non-FLAC file")
        part.replace(dest)
    finally:
        part.unlink(missing_ok=True)
    return dest


def _outcome(track: TidalTrack, path: Path, query: str, elapsed: float) -> DownloadOutcome:
    return DownloadOutcome(
        path=path,
        tier=1,
        source="tidal",
        elapsed_s=elapsed,
        query=query,
        title=track.title,
        artist=track.artist,
        album=track.album,
        track_no=track.track_no,
        disc_no=track.disc_no,
        art_url=track.cover_url,
    )


def tidal_download(
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
    path = _fetch_track(track, out_dir, fetch)
    return _outcome(track, path, query, clock() - start)


def search_candidates(
    query: str, want: TrackIdentity | None = None, *, search: Search
) -> list[Candidate]:
    title_query = want.title if want and want.title else query
    return [
        Candidate(
            source="tidal",
            tier=1,
            ref=track.id,
            title=track.title,
            artist=track.artist,
            album=track.album,
            quality_label="Hi-Res FLAC" if "HI_RES" in track.quality else "FLAC",
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
    del want
    start = clock()
    track = TidalTrack(
        id=candidate.ref,
        title=candidate.title,
        artist=candidate.artist,
        album=candidate.album,
        duration_s=candidate.duration_s,
        isrc=None,
        cover_url=candidate.art_url,
        track_no=None,
        disc_no=None,
        quality=candidate.quality_label,
    )
    path = _fetch_track(track, out_dir, fetch)
    return _outcome(track, path, f"{track.artist} - {track.title}", clock() - start)
