"""arcod.xyz client for the Tier-2 adapter.

arcod is a public Qobuz proxy: no credentials, requests only carry a
``Referer: https://arcod.xyz/`` and a desktop browser ``User-Agent``. Downloads
are async jobs — create one, poll it to ``completed``, then stream the delivered
file from ``dl.arcod.xyz``. All network I/O; excluded from coverage — the parse,
payload-building, selection and poll-loop logic live in the adapter and are
tested there.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx

_CHUNK = 64 * 1024  # 64 KiB — stream the audio file to disk, never buffer the whole thing
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


class ArcodClient:  # pragma: no cover - network I/O
    def __init__(self, base_url: str = "https://arcod.xyz") -> None:
        self._base = base_url.rstrip("/")
        self._http = httpx.Client(
            timeout=30.0,
            headers={"Referer": "https://arcod.xyz/", "User-Agent": _UA},
            follow_redirects=True,
        )

    def search(self, query: str) -> dict[str, Any]:
        resp = self._http.get(f"{self._base}/api/get-music", params={"q": query, "offset": 0})
        resp.raise_for_status()
        return dict(resp.json())

    def create_download(self, payload: dict[str, Any]) -> dict[str, Any]:
        resp = self._http.post(f"{self._base}/api/v2/downloads", json=payload)
        resp.raise_for_status()
        return dict(resp.json())

    def poll(self, job_id: str) -> dict[str, Any]:
        resp = self._http.get(f"{self._base}/api/v2/downloads/{job_id}")
        resp.raise_for_status()
        return dict(resp.json())

    def fetch(self, url: str, dest: Path) -> None:
        """Stream the audio at ``url`` (on ``dl.arcod.xyz``) to ``dest`` in 64 KiB chunks."""
        with self._http.stream("GET", url, timeout=120.0) as resp:
            resp.raise_for_status()
            with dest.open("wb") as fh:
                for chunk in resp.iter_bytes(_CHUNK):
                    fh.write(chunk)

    def close(self) -> None:
        self._http.close()
