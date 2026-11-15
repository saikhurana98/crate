"""HTTP client for Monochrome's public Tracks API."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx

DEFAULT_BASE = "https://tracks.monochrome.st"
_CHUNK = 64 * 1024
_FETCH_ATTEMPTS = 4


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
        """Stream one FLAC, resuming when Monochrome closes a large response early."""
        if not track_id.isdecimal():
            raise ValueError("invalid Monochrome track ID")
        url = f"{self._base}/track/{track_id}"
        last_error: Exception | None = None
        for _attempt in range(_FETCH_ATTEMPTS):
            offset = dest.stat().st_size if dest.exists() else 0
            headers = {"Range": f"bytes={offset}-"} if offset else {}
            try:
                with self._http.stream("GET", url, headers=headers, timeout=120.0) as resp:
                    resp.raise_for_status()
                    append = offset > 0 and resp.status_code == httpx.codes.PARTIAL_CONTENT
                    if append:
                        content_range = resp.headers.get("content-range", "")
                        if not content_range.startswith(f"bytes {offset}-"):
                            raise RuntimeError("Monochrome returned an invalid resume range")
                    mode = "ab" if append else "wb"
                    # Unbuffered writes make the last received byte immediately
                    # visible to the next Range request after a transport error.
                    with dest.open(mode, buffering=0) as fh:
                        for chunk in resp.iter_bytes(_CHUNK):
                            fh.write(chunk)
                return
            except httpx.TransportError as exc:
                last_error = exc
        raise RuntimeError(f"Monochrome download remained incomplete after retries: {last_error}")

    def close(self) -> None:
        self._http.close()
