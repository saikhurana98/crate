"""Tier-2 source adapter: arcod.xyz -> (requested) FLAC via an async job.

arcod is a public **Qobuz proxy** with no auth (the client only sends a Referer +
browser User-Agent). Unlike qobuz/deezer the download is a **job**: create it,
poll to ``completed``, then stream the delivered file. arcod *may* downgrade the
FLAC request to MP3 (observed ~12-18 MB .mp3 despite ``format=FLAC``), so this
source is wired ``expected_lossless=False`` — request FLAC, accept whatever the
prober reports.

Pure and testable here: search parsing, the POST payload, track selection, and
the poll-loop orchestration. Every IO collaborator (search / create_download /
poll / fetch / clock / sleep) is injected.
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
from crate.domain.matching import artist_matches, duration_prefilter, match_score, title_matches

Search = Callable[[str], Any]  # query -> arcod get-music JSON
CreateDownload = Callable[[dict[str, Any]], Any]  # payload -> create-job JSON ({"id": uuid, ...})
Poll = Callable[[str], Any]  # job id -> status JSON
Fetch = Callable[[str, Path], None]  # (downloadUrl, dest) -> stream the file to dest
Clock = Callable[[], float]
Sleep = Callable[[float], None]

# quality 27 = Qobuz hi-res FLAC (best-effort; arcod may downgrade to MP3).
FORMAT_HIRES = 27

# Async job poll discipline: bound the wait so a stuck job raises and the cascade
# advances (Phase-0 timeout discipline). Module constants rather than parameters
# so the injected-callable signature stays within the ruff max-args budget.
POLL_TIMEOUT_S = 180.0
POLL_INTERVAL_S = 2.0

_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


@dataclass(frozen=True, slots=True)
class ArcodTrack:
    id: int
    title: str
    artist: str
    artist_id: str
    album_id: str
    album_title: str
    cover_url: str
    release_date: str
    isrc: str | None
    duration_s: int | None
    track_no: int | None
    hires: bool


def parse_search(data: dict[str, Any]) -> list[ArcodTrack]:
    """Parse an arcod ``/api/get-music`` response into tracks.

    Raises ``LookupError`` when the API reports failure (``success: false``)."""
    if data.get("success") is False:
        raise LookupError(f"arcod search failed: {data.get('error')}")
    tracks: list[ArcodTrack] = []
    for item in data.get("data", {}).get("tracks", {}).get("items", []):
        performer = item.get("performer") or {}
        album = item.get("album") or {}
        image = album.get("image") or {}
        tracks.append(
            ArcodTrack(
                id=int(item["id"]),
                title=str(item.get("title", "Unknown")),
                artist=str(performer.get("name", "Unknown")),
                artist_id=str(performer.get("id", "")),
                album_id=str(album.get("id", "")),
                album_title=str(album.get("title", "")),
                cover_url=str(image.get("large", "")),
                release_date=str(album.get("release_date_original", "")),
                isrc=item.get("isrc"),
                duration_s=item.get("duration"),
                track_no=item.get("track_number"),
                hires=bool(item.get("hires", False)),
            )
        )
    return tracks


def build_download_payload(track: ArcodTrack) -> dict[str, Any]:
    """The ``POST /api/v2/downloads`` body for a single track (Qobuz-shaped)."""
    return {
        "albumId": track.album_id,
        "trackId": str(track.id),
        "albumTitle": track.album_title,
        "artistName": track.artist,
        "artistId": track.artist_id,
        "coverUrl": track.cover_url,
        "releaseDate": track.release_date,
        "tracksCount": 1,
        "quality": FORMAT_HIRES,
        "format": "FLAC",
        "bitrate": 320,
        "embedLyrics": True,
        "lyricsMode": "embed",
        "downloadBooklet": False,
        "attachCover": False,
        "zipName": "{artists} - {name}",
        "trackName": "{track} - {name}",
    }


def select_track(
    tracks: list[ArcodTrack],
    query: str,
    want: TrackIdentity | None = None,
) -> ArcodTrack:
    """Prefer an exact ISRC match; else the best title+artist match.

    Same gate order as qobuz/deezer: an exact ISRC (from ``want``) short-circuits
    everything (it uniquely identifies the recording, so a slightly-off duration
    must not veto it). Otherwise candidates pass a title gate, then (when the
    wanted artist is known) an artist gate that drops a same-title track by a
    different artist, then the duration prefilter, and the best title+artist match
    wins so a requested song isn't swapped for an unrelated track sharing the title."""
    want_isrc = want.isrc if want else None
    want_artist = want.artist if want else None
    want_duration = want.duration_s if want else None
    if want_isrc:
        for track in tracks:
            if track.isrc and track.isrc.upper() == want_isrc.upper():
                return track
    matching = [t for t in tracks if title_matches(t.title, query)]
    if want_artist:
        matching = [t for t in matching if artist_matches(t.artist, want_artist)]
    if not matching:
        raise LookupError("no title-matching arcod track")
    pool = duration_prefilter(matching, want_duration, lambda t: t.duration_s)
    return max(pool, key=lambda t: match_score(t.title, t.artist, query))


def arcod_download(
    query: str,
    out_dir: Path,
    want: TrackIdentity | None = None,
    *,
    search: Search,
    create_download: CreateDownload,
    poll: Poll,
    fetch: Fetch,
    clock: Clock = time.monotonic,
    sleep: Sleep = time.sleep,
) -> DownloadOutcome:
    """Search arcod, create a job, poll it to completion, stream the file to disk.

    ``want`` (when known) supplies the ISRC short-circuit + duration prefilter for
    selection. The job is async: poll on a bounded monotonic deadline, raising on
    ``failed`` (with the reported error) or on timeout so the cascade advances. The
    delivered file streams to a ``.part`` sidecar and renames on success, so an
    interrupted download never leaves a half file. The extension is taken from the
    server's ``fileName`` (arcod may deliver .mp3 despite the FLAC request)."""
    start = clock()
    track = select_track(parse_search(search(query)), query, want)

    job = create_download(build_download_payload(track))
    job_id = str(job["id"])

    deadline = clock() + POLL_TIMEOUT_S
    status: dict[str, Any] = {}
    while True:
        status = dict(poll(job_id))
        state = status.get("status")
        if state == "completed":
            break
        if state == "failed":
            raise RuntimeError(f"arcod job {job_id} failed: {status.get('error')}")
        if clock() >= deadline:
            raise RuntimeError(f"arcod job {job_id} timed out (last status: {state})")
        sleep(POLL_INTERVAL_S)

    download_url = status["downloadUrl"]
    ext = Path(str(status.get("fileName", ""))).suffix or ".flac"
    name = _UNSAFE.sub("_", f"{track.artist} - {track.title} [{track.id}]") + ext
    dest = out_dir / name
    part = dest.with_name(dest.name + ".part")
    try:
        fetch(download_url, part)
        part.replace(dest)
    finally:
        part.unlink(missing_ok=True)
    return DownloadOutcome(
        path=dest,
        tier=2,
        source="arcod",
        elapsed_s=clock() - start,
        query=query,
        title=track.title,
        artist=track.artist,
        album=track.album_title or None,
        track_no=track.track_no,
    )
