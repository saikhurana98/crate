"""Deezer's public, keyless API (``api.deezer.com``): track lookup by ISRC or id.

Library Sync previews use it: every Deezer track carries a ~30 s MP3 ``preview``
on Deezer's CDN. Preview URLs are signed and expire, so callers cache the Deezer
*id* and fetch a fresh URL each time. Needs no ARL.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

API = "https://api.deezer.com"
_ISRC = re.compile(r"^[A-Z]{2}[A-Z0-9]{3}\d{7}$")

GetJson = Callable[[str], Any]


class DeezerPublic:
    """Track reads over an injected ``get_json(url)`` (network in composition)."""

    def __init__(self, get_json: GetJson) -> None:
        self._get = get_json

    def _track(self, path: str) -> dict[str, Any] | None:
        data = self._get(f"{API}/track/{path}")
        if not isinstance(data, dict) or data.get("error") or not data.get("id"):
            return None  # Deezer answers 200 + {"error": {...}} for unknown tracks
        return data

    def track_by_isrc(self, isrc: str) -> dict[str, Any] | None:
        """The Deezer track for ``isrc``; None if unknown or not a well-formed ISRC
        (checked first, so a bad value never reaches the URL)."""
        code = isrc.strip().upper()
        return self._track(f"isrc:{code}") if _ISRC.match(code) else None

    def track(self, deezer_id: str) -> dict[str, Any] | None:
        """The Deezer track with this numeric id, or None."""
        ref = str(deezer_id).strip()
        return self._track(ref) if ref.isdigit() else None
