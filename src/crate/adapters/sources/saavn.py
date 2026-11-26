"""JioSaavn candidate search and AAC acquisition."""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from html import unescape
from pathlib import Path
from typing import Any

from crate.domain.candidate import Candidate
from crate.domain.download import DownloadOutcome
from crate.domain.identity import TrackIdentity
from crate.domain.matching import artist_matches, duration_prefilter, match_score, title_matches

Search = Callable[[str], list[dict[str, Any]]]
Fetch = Callable[[str, Path], None]
TRACK_ID = re.compile(r"[A-Za-z0-9_-]{1,32}", re.ASCII)


def search_candidates(query: str, want: TrackIdentity | None, *, search: Search) -> list[Candidate]:
    candidates = []
    title_query = want.title if want and want.title else query
    for row in search(query):
        track_id = str(row.get("id", ""))
        title = unescape(str(row.get("name") or ""))
        artist = ", ".join(
            unescape(str(item["name"]))
            for item in (row.get("artists") or {}).get("primary") or []
            if item.get("name")
        )
        if not TRACK_ID.fullmatch(track_id) or not title_matches(title, title_query):
            continue
        if want and want.artist and not artist_matches(artist, want.artist):
            continue
        images = row.get("image") or []
        cover = str(images[-1].get("url", "")) if images else ""
        duration = str(row.get("duration", ""))
        album = (row.get("album") or {}).get("name")
        candidates.append(
            Candidate(
                "saavn",
                2,
                track_id,
                title,
                artist,
                unescape(album) if album else None,
                "AAC",
                False,
                int(duration) if duration.isdecimal() else None,
                None,
                art_url=cover if cover.startswith("https://c.saavncdn.com/") else None,
            )
        )
    return candidates


def download_candidate(candidate: Candidate, out_dir: Path, *, fetch: Fetch) -> DownloadOutcome:
    if not TRACK_ID.fullmatch(candidate.ref):
        raise ValueError("invalid JioSaavn track ID")
    start = time.monotonic()
    path = out_dir / f"saavn-{candidate.ref}.m4a"
    part = path.with_suffix(".m4a.part")
    try:
        fetch(candidate.ref, part)
        part.replace(path)
    finally:
        part.unlink(missing_ok=True)
    return DownloadOutcome(
        path,
        2,
        "saavn",
        time.monotonic() - start,
        f"{candidate.artist} {candidate.title}",
        artist=candidate.artist,
        title=candidate.title,
        album=candidate.album,
        art_url=candidate.art_url,
    )


def download(
    query: str, out_dir: Path, want: TrackIdentity | None, *, search: Search, fetch: Fetch
) -> DownloadOutcome:
    matches = search_candidates(query, want, search=search)
    if not matches:
        raise LookupError("no title/artist-matching JioSaavn track")
    pool = duration_prefilter(matches, want.duration_s if want else None, lambda c: c.duration_s)
    best = max(pool, key=lambda c: match_score(c.title, c.artist, query))
    return download_candidate(best, out_dir, fetch=fetch)
