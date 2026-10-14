"""Public Octave album-catalog client used by bulk imports."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

import httpx

DEFAULT_BASE = "https://music.octavestreaming.com"
_ALBUM_PATH_PARTS = 2


def album_id(ref: str) -> str:
    """Return the numeric album id from a trusted Octave album URL."""
    parsed = urlparse(ref.strip())
    if parsed.scheme != "https" or (parsed.hostname or "").lower() != "music.octavestreaming.com":
        raise ValueError("invalid Octave album URL")
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) != _ALBUM_PATH_PARTS or parts[0] != "album" or not parts[1].isdecimal():
        raise ValueError("invalid Octave album URL")
    return parts[1]


class OctaveClient:
    """Resolve an Octave album URL into cascade-ready track identities."""

    def __init__(
        self,
        base_url: str = DEFAULT_BASE,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._base = base_url.rstrip("/")
        resilient_transport = transport or httpx.HTTPTransport(retries=2)
        self._http = httpx.Client(
            timeout=20.0,
            follow_redirects=True,
            transport=resilient_transport,
            headers={"Accept": "application/json", "User-Agent": "crate/0.1"},
        )

    def album(self, ref: str) -> dict[str, Any]:
        ident = album_id(ref)
        response = self._http.get(f"{self._base}/api/album/{ident}")
        response.raise_for_status()
        payload = response.json()
        album = payload.get("album") if isinstance(payload, dict) else None
        if not isinstance(album, dict):
            raise ValueError("Octave returned an invalid album")

        tracks: list[dict[str, Any]] = []
        for item in album.get("tracks") or []:
            if not isinstance(item, dict) or not item.get("title"):
                continue
            artist_data = item.get("artist") or {}
            artist = str(artist_data.get("name") or "").strip()
            title = str(item["title"]).strip()
            duration = int(item.get("duration") or 0)
            tracks.append(
                {
                    "query": f"{artist} {title}".strip(),
                    "artist": artist or None,
                    "title": title,
                    "isrc": None,
                    "duration_s": duration or None,
                }
            )

        album_artist = album.get("artist") or {}
        return {
            "name": str(album.get("title") or "Octave album"),
            "owner": str(album_artist.get("name") or ""),
            "tracks": tracks,
        }
