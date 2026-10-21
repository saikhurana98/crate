"""Octave catalog adapter using the site's authenticated download flow."""

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
class OctaveTrack:
    id: str
    title: str
    artist: str
    album: str | None
    duration_s: int | None
    cover_url: str | None


def _integer(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def parse_search(data: dict[str, Any]) -> list[OctaveTrack]:
    tracks: list[OctaveTrack] = []
    for item in data.get("tracks") or data.get("results") or []:
        if not isinstance(item, dict):
            continue
        track_id = str(item.get("id") or "")
        title = str(item.get("title") or "").strip()
        artist_data = item.get("artist") or {}
        artist = str(artist_data.get("name") or "").strip()
        if not track_id.isdecimal() or not title or not artist:
            continue
        album = item.get("album") or {}
        cover = album.get("cover_xl") or album.get("cover_big") or album.get("cover_medium")
        tracks.append(
            OctaveTrack(
                id=track_id,
                title=title,
                artist=artist,
                album=str(album.get("title")) if album.get("title") else None,
                duration_s=_integer(item.get("duration")),
                cover_url=str(cover) if cover else None,
            )
        )
    return tracks


def select_track(tracks: list[OctaveTrack], query: str, want: TrackIdentity | None) -> OctaveTrack:
    title_query = want.title if want and want.title else query
    matches = [track for track in tracks if title_matches(track.title, title_query)]
    if want and want.artist:
        matches = [track for track in matches if artist_matches(track.artist, want.artist)]
    if not matches:
        raise LookupError("no title/artist-matching Octave track")
    pool = duration_prefilter(matches, want.duration_s if want else None, lambda t: t.duration_s)
    return max(pool, key=lambda t: match_score(t.title, t.artist, query))


def _candidate(track: OctaveTrack) -> Candidate:
    return Candidate(
        source="octave",
        tier=1,
        ref=track.id,
        title=track.title,
        artist=track.artist,
        album=track.album,
        quality_label="FLAC via Octave",
        lossless=True,
        duration_s=track.duration_s,
        size_bytes=None,
        art_url=track.cover_url,
    )


def search_candidates(
    query: str,
    want: TrackIdentity | None = None,
    *,
    search: Search,
) -> list[Candidate]:
    tracks = parse_search(search(query))
    if want is not None:
        try:
            tracks = [select_track(tracks, query, want)]
        except LookupError:
            return []
    return [_candidate(track) for track in tracks]


def _download(candidate: Candidate, out_dir: Path, fetch: Fetch) -> Path:
    stem = _UNSAFE.sub("_", f"{candidate.artist} - {candidate.title} [{candidate.ref}]")
    dest = out_dir / f"{stem}.flac"
    part = out_dir / f"{stem}.flac.part"
    try:
        fetch(candidate.ref, part)
        with part.open("rb") as fh:
            if fh.read(4) != b"fLaC":
                raise ValueError("Octave returned a non-FLAC file")
        part.replace(dest)
    finally:
        part.unlink(missing_ok=True)
    return dest


def download_candidate(
    candidate: Candidate,
    out_dir: Path,
    want: TrackIdentity | None = None,
    *,
    fetch: Fetch,
    clock: Clock = time.monotonic,
) -> DownloadOutcome:
    start = clock()
    path = _download(candidate, out_dir, fetch)
    return DownloadOutcome(
        path=path,
        tier=1,
        source="octave",
        elapsed_s=clock() - start,
        query=f"{candidate.artist} - {candidate.title}",
        title=candidate.title,
        artist=candidate.artist,
        album=candidate.album,
        art_url=candidate.art_url,
    )


def octave_download(
    query: str,
    out_dir: Path,
    want: TrackIdentity | None = None,
    *,
    search: Search,
    fetch: Fetch,
    clock: Clock = time.monotonic,
) -> DownloadOutcome:
    track = select_track(parse_search(search(query)), query, want)
    return download_candidate(_candidate(track), out_dir, want, fetch=fetch, clock=clock)
