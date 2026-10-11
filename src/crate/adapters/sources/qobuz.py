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

from crate.domain.candidate import Candidate
from crate.domain.download import DownloadOutcome
from crate.domain.identity import TrackIdentity
from crate.domain.matching import artist_matches, duration_prefilter, match_score, title_matches

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
    cover_url: str | None = None


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
        album = item.get("album") or {}
        image = album.get("image") or {}
        tracks.append(
            QobuzTrack(
                id=int(item["id"]),
                title=str(item.get("title", "Unknown")),
                artist=str(performer.get("name", "Unknown")),
                album=str(album.get("title", "")),
                duration_s=item.get("duration"),
                bit_depth=item.get("maximum_bit_depth"),
                sample_rate_khz=item.get("maximum_sampling_rate"),
                isrc=item.get("isrc"),
                cover_url=image.get("large") or None,
            )
        )
    return tracks


def select_track(
    tracks: list[QobuzTrack],
    query: str,
    isrc: str | None = None,
    want_duration_s: int | None = None,
    want_artist: str | None = None,
) -> QobuzTrack:
    """Prefer an exact ISRC match; else the best title+artist match.

    An exact ISRC short-circuits everything (it uniquely identifies the recording,
    so a slightly-off search duration must not veto it). Otherwise candidates pass
    a title gate, then (when ``want_artist`` is known) an artist gate that drops a
    same-title track by a different artist, then the duration prefilter, and the
    title+artist-best survivor is picked — so a requested song isn't swapped for an
    unrelated track that merely shares the title."""
    if isrc:
        for track in tracks:
            if track.isrc and track.isrc.upper() == isrc.upper():
                return track
    matching = [t for t in tracks if title_matches(t.title, query)]
    if want_artist:
        matching = [t for t in matching if artist_matches(t.artist, want_artist)]
    if not matching:
        raise LookupError("no title-matching Qobuz track")
    pool = duration_prefilter(matching, want_duration_s, lambda t: t.duration_s)
    return max(pool, key=lambda t: match_score(t.title, t.artist, query))


def _quality_label(track: QobuzTrack) -> str:
    """Human-readable quality badge for a candidate row (e.g. ``FLAC 24/96``)."""
    if track.bit_depth and track.sample_rate_khz:
        return f"FLAC {track.bit_depth}/{track.sample_rate_khz:g}"
    return "FLAC"


def _download_ref(
    track_id: int,
    artist: str,
    title: str,
    out_dir: Path,
    *,
    get_file_url: GetFileUrl,
    fetch: Fetch,
) -> Path:
    """Download-by-id core shared by the cascade path and the review re-download:
    resolve the signed url for ``track_id`` and stream it to disk (already FLAC),
    skipping search + selection."""
    name = _UNSAFE.sub("_", f"{artist} - {title} [{track_id}]") + ".flac"
    dest = out_dir / name
    part = dest.with_name(dest.name + ".part")
    try:
        fetch(get_file_url(track_id), part)
        part.replace(dest)
    finally:
        part.unlink(missing_ok=True)
    return dest


def qobuz_download(
    query: str,
    out_dir: Path,
    want: TrackIdentity | None = None,
    *,
    search: Search,
    get_file_url: GetFileUrl,
    fetch: Fetch,
    isrc: str | None = None,
    clock: Clock = time.monotonic,
) -> DownloadOutcome:
    """Search Qobuz, resolve the signed FLAC url, stream it to disk (already FLAC).

    ``want`` (when known) supplies the ISRC for an exact short-circuit and the
    duration for the candidate prefilter. Streams to a ``.part`` sidecar and
    renames on success, so an interrupted download never leaves a half file that a
    later probe would treat as real."""
    start = clock()
    want_isrc = isrc or (want.isrc if want else None)
    want_duration = want.duration_s if want else None
    want_artist = want.artist if want else None
    track = select_track(parse_search(search(query)), query, want_isrc, want_duration, want_artist)

    dest = _download_ref(
        track.id, track.artist, track.title, out_dir, get_file_url=get_file_url, fetch=fetch
    )
    return DownloadOutcome(
        path=dest,
        tier=1,
        source="qobuz",
        elapsed_s=clock() - start,
        query=query,
        title=track.title,
        artist=track.artist,
        album=track.album or None,
        art_url=track.cover_url,
    )


def search_candidates(
    query: str,
    want: TrackIdentity | None = None,
    *,
    search: Search,
) -> list[Candidate]:
    """Every title-matching Qobuz result as a :class:`Candidate` (no download).

    Reuses ``parse_search`` + the shared title gate but does NOT collapse to one
    via ``select_track`` — ranking is left to the API's ``candidate_match``. Each
    candidate's ``ref`` is the Qobuz track id, enough for ``download_candidate``
    to fetch it later without re-searching."""
    tracks = parse_search(search(query))
    return [
        Candidate(
            source="qobuz",
            tier=1,
            ref=str(t.id),
            title=t.title,
            artist=t.artist,
            album=t.album or None,
            quality_label=_quality_label(t),
            lossless=True,
            duration_s=t.duration_s,
            size_bytes=None,
            art_url=t.cover_url,
        )
        for t in tracks
        if title_matches(t.title, query)
    ]


def download_candidate(
    candidate: Candidate,
    out_dir: Path,
    want: TrackIdentity | None = None,
    *,
    get_file_url: GetFileUrl,
    fetch: Fetch,
    clock: Clock = time.monotonic,
) -> DownloadOutcome:
    """Fetch exactly ``candidate`` (by its Qobuz track-id ``ref``) — no search."""
    start = clock()
    dest = _download_ref(
        int(candidate.ref),
        candidate.artist,
        candidate.title,
        out_dir,
        get_file_url=get_file_url,
        fetch=fetch,
    )
    return DownloadOutcome(
        path=dest,
        tier=1,
        source="qobuz",
        elapsed_s=clock() - start,
        query=f"{candidate.artist} - {candidate.title}",
        title=candidate.title,
        artist=candidate.artist,
        album=candidate.album,
        art_url=candidate.art_url,
    )
