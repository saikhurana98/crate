"""HTTP helpers (httpx) injected into source adapters. Thin I/O wrappers."""

from __future__ import annotations

from typing import Any

import httpx


def get_json(
    url: str, params: dict[str, Any], headers: dict[str, str] | None = None
) -> Any:  # pragma: no cover
    resp = httpx.get(url, params=params, headers=headers or {}, timeout=30.0, follow_redirects=True)
    resp.raise_for_status()
    return resp.json()


def get_bytes(url: str) -> bytes:  # pragma: no cover
    resp = httpx.get(url, timeout=120.0, follow_redirects=True)
    resp.raise_for_status()
    return resp.content
