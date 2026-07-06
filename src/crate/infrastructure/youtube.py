"""YouTube playlist resolution via yt-dlp (flat — no downloads).

Extracts the playlist title + entry titles so the cascade can search each track
across all tiers (yt-dlp is only the Tier-3 source; the playlist itself is just a
track list here).
"""

from __future__ import annotations

from typing import Any


def resolve_youtube_playlist(url: str, max_tracks: int = 500) -> dict[str, Any]:  # pragma: no cover
    # lazy: heavy, only needed for a YouTube bulk import
    import yt_dlp  # type: ignore[import-untyped]  # noqa: PLC0415

    opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": True,
        "skip_download": True,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    name = str(info.get("title") or "YouTube playlist")
    tracks = [
        str(e["title"])
        for e in (info.get("entries") or [])
        if e and e.get("title") and e.get("title") not in ("[Deleted video]", "[Private video]")
    ]
    return {"name": name, "owner": "", "tracks": tracks[:max_tracks]}
