"""HTTP helpers (httpx) injected into source adapters. Thin I/O wrappers."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx

_CHUNK = 64 * 1024  # 64 KiB — stream large audio files to disk, never buffer whole


def get_json(
    url: str, params: dict[str, Any], headers: dict[str, str] | None = None
) -> Any:  # pragma: no cover
    resp = httpx.get(url, params=params, headers=headers or {}, timeout=30.0, follow_redirects=True)
    resp.raise_for_status()
    return resp.json()


def stream_to(url: str, dest: Path, *, timeout: float = 120.0) -> int:  # pragma: no cover
    """Stream ``url`` to ``dest`` in 64 KiB chunks; return the bytes written.

    Avoids buffering the whole response body in RAM — the point of P0-A. Opens the
    stream, checks the status from the response headers, then writes chunk by chunk."""
    written = 0
    with httpx.stream("GET", url, timeout=timeout, follow_redirects=True) as resp:
        resp.raise_for_status()
        with dest.open("wb") as fh:
            for chunk in resp.iter_bytes(_CHUNK):
                fh.write(chunk)
                written += len(chunk)
    return written
