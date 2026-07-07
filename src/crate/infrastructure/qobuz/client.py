"""Qobuz API client for the Tier-1 adapter.

Holds the app id/secret + user auth token (all user-supplied via config; no
password handling here) and signs track/getFileUrl. Downloads are plain FLAC —
no decryption. All network I/O; excluded from coverage (signing/parse are in the
adapter and tested there).
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import httpx

from crate.adapters.sources.qobuz import FORMAT_HIRES, sign_get_file_url

_CHUNK = 64 * 1024  # 64 KiB — stream the FLAC to disk, never buffer the whole file
_BASE = "https://www.qobuz.com/api.json/0.2"
# hi-res 24/192, hi-res 24/96, CD 16/44.1 — try best-first
_FORMATS = (FORMAT_HIRES, 7, 6)


class QobuzClient:  # pragma: no cover - network I/O
    def __init__(self, app_id: str, app_secret: str, user_auth_token: str) -> None:
        self._app_id = app_id
        self._secret = app_secret
        self._http = httpx.Client(
            base_url=_BASE,
            headers={"X-App-Id": app_id, "X-User-Auth-Token": user_auth_token},
            timeout=30.0,
        )

    def search(self, query: str) -> dict[str, Any]:
        resp = self._http.get(
            "/track/search", params={"query": query, "limit": 10, "app_id": self._app_id}
        )
        resp.raise_for_status()
        return dict(resp.json())

    def get_file_url(self, track_id: int) -> str:
        last: dict[str, Any] = {}
        for fmt in _FORMATS:
            ts = int(time.time())
            resp = self._http.get(
                "/track/getFileUrl",
                params={
                    "request_ts": ts,
                    "request_sig": sign_get_file_url(track_id, fmt, ts, self._secret),
                    "track_id": track_id,
                    "format_id": fmt,
                    "intent": "stream",
                    "app_id": self._app_id,
                },
            )
            resp.raise_for_status()
            body = resp.json()
            if body.get("url") and not body.get("sample"):
                return str(body["url"])
            last = body
        raise RuntimeError(f"no full FLAC stream for {track_id} (subscription/token?): {last}")

    def fetch(self, url: str, dest: Path) -> None:
        """Stream the FLAC at ``url`` to ``dest`` in 64 KiB chunks (no full buffer)."""
        with self._http.stream("GET", url, timeout=120.0) as resp:
            resp.raise_for_status()
            with dest.open("wb") as fh:
                for chunk in resp.iter_bytes(_CHUNK):
                    fh.write(chunk)

    def close(self) -> None:
        self._http.close()
