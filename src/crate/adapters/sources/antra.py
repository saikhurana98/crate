"""Antra adapter: submit track metadata to its multi-source lossless resolver."""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from crate.domain.candidate import Candidate
from crate.domain.download import DownloadOutcome
from crate.domain.identity import TrackIdentity, from_query

Download = Callable[[dict[str, Any], Path], None]
Clock = Callable[[], float]
_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _identity(query: str, want: TrackIdentity | None) -> TrackIdentity:
    parsed = from_query(query)
    return TrackIdentity(
        artist=(want.artist if want else None) or parsed.artist,
        title=(want.title if want else None) or parsed.title,
        isrc=want.isrc if want else None,
        duration_s=want.duration_s if want else None,
    )


def _track(identity: TrackIdentity, album: str | None = None) -> dict[str, Any]:
    if not identity.artist or not identity.title:
        raise LookupError("Antra needs artist and title metadata (use 'Artist - Title')")
    return {
        "title": identity.title,
        "artists": [identity.artist],
        "album": album or "",
        "isrc": identity.isrc,
        "duration_ms": identity.duration_s * 1000 if identity.duration_s is not None else None,
        "track_number": None,
        "disc_number": None,
        "release_year": None,
    }


def search_candidates(query: str, want: TrackIdentity | None = None) -> list[Candidate]:
    """Return Antra's resolver target without spending quota during search."""
    identity = _identity(query, want)
    try:
        track = _track(identity)
    except LookupError:
        return []
    return [
        Candidate(
            source="antra",
            tier=1,
            ref=json.dumps(track, separators=(",", ":"), sort_keys=True),
            title=str(track["title"]),
            artist=str(track["artists"][0]),
            album=None,
            quality_label="FLAC 16-bit via Antra",
            lossless=True,
            duration_s=identity.duration_s,
            size_bytes=None,
        )
    ]


def _download(
    candidate: Candidate,
    out_dir: Path,
    download: Download,
) -> Path:
    track = json.loads(candidate.ref)
    if not isinstance(track, dict):
        raise ValueError("invalid Antra candidate reference")
    stem = _UNSAFE.sub("_", f"{candidate.artist} - {candidate.title}")
    dest = out_dir / f"{stem}.flac"
    part = out_dir / f"{stem}.flac.part"
    try:
        download(track, part)
        with part.open("rb") as fh:
            if fh.read(4) != b"fLaC":
                raise ValueError("Antra returned a non-FLAC file")
        part.replace(dest)
    finally:
        part.unlink(missing_ok=True)
    return dest


def download_candidate(
    candidate: Candidate,
    out_dir: Path,
    want: TrackIdentity | None = None,
    *,
    download: Download,
    clock: Clock = time.monotonic,
) -> DownloadOutcome:
    start = clock()
    path = _download(candidate, out_dir, download)
    return DownloadOutcome(
        path=path,
        tier=1,
        source="antra",
        elapsed_s=clock() - start,
        query=f"{candidate.artist} - {candidate.title}",
        title=candidate.title,
        artist=candidate.artist,
        album=candidate.album,
    )


def antra_download(
    query: str,
    out_dir: Path,
    want: TrackIdentity | None = None,
    *,
    download: Download,
    clock: Clock = time.monotonic,
) -> DownloadOutcome:
    candidates = search_candidates(query, want)
    if not candidates:
        raise LookupError("Antra needs artist and title metadata (use 'Artist - Title')")
    return download_candidate(candidates[0], out_dir, want, download=download, clock=clock)
