"""HTTP client for Monochrome's public Tracks API."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx

DEFAULT_BASE = "https://tracks.monochrome.st"
_CHUNK = 64 * 1024


class MonochromeClient:  # pragma: no cover - network I/O
    def __init__(
        self, base_url: str = DEFAULT_BASE, *, transport: httpx.BaseTransport | None = None
    ) -> None:
        self._base = base_url.rstrip("/")
        # HTTPTransport retries transient connection failures, including temporary
        # DNS resolution errors. Monochrome sits behind Cloudflare and both
        # production hosts have occasionally observed EAI_AGAIN/EAI_NONAME from
        # their local resolvers; a single miss should not discard the source.
        resilient_transport = transport or httpx.HTTPTransport(retries=2)
        self._http = httpx.Client(
            timeout=20.0, follow_redirects=True, transport=resilient_transport
        )

    def search(self, query: str) -> dict[str, Any]:
        resp = self._http.get(f"{self._base}/search/tracks", params={"q": query, "limit": 20})
        resp.raise_for_status()
        return dict(resp.json())

    def fetch(self, track_id: str, dest: Path) -> None:
        """Stream one FLAC directly; do not make a preceding HEAD request."""
        if not track_id.isdecimal():
            raise ValueError("invalid Monochrome track ID")
        with self._http.stream("GET", f"{self._base}/track/{track_id}", timeout=120.0) as resp:
            resp.raise_for_status()
            with dest.open("wb") as fh:
                for chunk in resp.iter_bytes(_CHUNK):
                    fh.write(chunk)

    def close(self) -> None:
        self._http.close()
