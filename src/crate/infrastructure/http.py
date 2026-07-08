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


def fetch_bytes(url: str, *, max_bytes: int = 4_000_000, timeout: float = 20.0) -> bytes:
    """Fetch a *small* resource (cover art) into memory, capped at ``max_bytes``.

    Streams in chunks and raises :class:`ValueError` the moment the accumulated
    body exceeds ``max_bytes``, so a mis-served huge file can never be buffered
    whole. For art only — never call this on an audio file (use
    :func:`stream_to`). The librarian catches every exception this raises, so an
    over-cap or failed art fetch never fails the download."""
    buf = bytearray()
    with httpx.stream("GET", url, timeout=timeout, follow_redirects=True) as resp:
        resp.raise_for_status()
        for chunk in resp.iter_bytes(_CHUNK):
            buf += chunk
            if len(buf) > max_bytes:
                raise ValueError(f"art exceeds {max_bytes} byte cap")
    return bytes(buf)


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
