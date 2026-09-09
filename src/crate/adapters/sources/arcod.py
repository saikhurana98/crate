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

import json
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

Search = Callable[[str], Any]  # query -> arcod get-music JSON
CreateDownload = Callable[[dict[str, Any]], Any]  # payload -> create-job JSON ({"id": uuid, ...})
Poll = Callable[[str, str], Any]  # (job id, access token) -> status JSON
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


def _stem(artist: str, title: str, track_id: int) -> str:
    """Sanitised ``artist - title [id]`` filename base (extension added by caller)."""
    return _UNSAFE.sub("_", f"{artist} - {title} [{track_id}]")


def _download_payload(
    payload: dict[str, Any],
    stem: str,
    out_dir: Path,
    *,
    create_download: CreateDownload,
    poll: Poll,
    fetch: Fetch,
    clock: Clock,
    sleep: Sleep,
) -> Path:
    """Download-by-payload core shared by the cascade path and the review
    re-download: create the async job for ``payload``, poll it to completion on a
    bounded deadline, then stream the delivered file to disk. ``stem`` is the
    sanitised ``artist - title [id]`` filename base (the extension is taken from
    the server's ``fileName``). No search — the payload fully identifies the
    track (its ``ref`` for a stored candidate)."""
    job = create_download(payload)
    job_id = str(job["id"])
    token = str(job["accessToken"])  # authorises the per-job status endpoint

    deadline = clock() + POLL_TIMEOUT_S
    status: dict[str, Any] = {}
    while True:
        status = dict(poll(job_id, token))
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
    dest = out_dir / (stem + ext)
    part = dest.with_name(dest.name + ".part")
    try:
        fetch(download_url, part)
        part.replace(dest)
    finally:
        part.unlink(missing_ok=True)
    return dest


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
    dest = _download_payload(
        build_download_payload(track),
        _stem(track.artist, track.title, track.id),
        out_dir,
        create_download=create_download,
        poll=poll,
        fetch=fetch,
        clock=clock,
        sleep=sleep,
    )
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
        art_url=track.cover_url or None,
    )


def _candidate_ref(track: ArcodTrack) -> str:
    """Pack the whole arcod download payload into the candidate's opaque ``ref``.

    arcod's job creation needs more than a bare track id (album id, artist id,
    cover, release date), so the ref carries the full payload as JSON. That keeps
    ``download_candidate`` genuinely search-free — it just replays the stored
    payload — while staying an opaque string from the caller's view."""
    return json.dumps(build_download_payload(track))


def search_candidates(
    query: str,
    want: TrackIdentity | None = None,
    *,
    search: Search,
) -> list[Candidate]:
    """Every title-matching arcod result as a :class:`Candidate` (no download).

    Reuses ``parse_search`` + the shared title gate (no ``select_track`` collapse).
    ``ref`` is the full download payload (JSON) so ``download_candidate`` re-creates
    the exact job without searching. arcod may downgrade FLAC to MP3, so
    ``lossless`` is left False (matching its ``expected_lossless=False`` wiring)."""
    tracks = parse_search(search(query))
    return [
        Candidate(
            source="arcod",
            tier=2,
            ref=_candidate_ref(t),
            title=t.title,
            artist=t.artist,
            album=t.album_title or None,
            quality_label="FLAC (proxy)",
            lossless=False,
            duration_s=t.duration_s,
            size_bytes=None,
            art_url=t.cover_url or None,
        )
        for t in tracks
        if title_matches(t.title, query)
    ]


def download_candidate(
    candidate: Candidate,
    out_dir: Path,
    want: TrackIdentity | None = None,
    *,
    create_download: CreateDownload,
    poll: Poll,
    fetch: Fetch,
    clock: Clock = time.monotonic,
    sleep: Sleep = time.sleep,
) -> DownloadOutcome:
    """Fetch exactly ``candidate`` by replaying its stored job payload — no search."""
    start = clock()
    payload = json.loads(candidate.ref)
    dest = _download_payload(
        payload,
        _stem(candidate.artist, candidate.title, int(payload["trackId"])),
        out_dir,
        create_download=create_download,
        poll=poll,
        fetch=fetch,
        clock=clock,
        sleep=sleep,
    )
    return DownloadOutcome(
        path=dest,
        tier=2,
        source="arcod",
        elapsed_s=clock() - start,
        query=f"{candidate.artist} - {candidate.title}",
        title=candidate.title,
        artist=candidate.artist,
        album=candidate.album,
    )
