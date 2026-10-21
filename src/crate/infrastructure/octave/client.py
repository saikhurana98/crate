"""Octave's public catalog and member-authorized download client."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

DEFAULT_BASE = "https://music.octavestreaming.com"
DEFAULT_API_BASE = "https://api.octavestreaming.com"
_ALBUM_PATH_PARTS = 2
_CHUNK = 64 * 1024
_TOKEN_SKEW_S = 30
_UNAUTHORIZED = 401


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
        api_base: str = DEFAULT_API_BASE,
        *,
        account_key: str | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._api = api_base.rstrip("/")
        self._account_key = account_key
        self._token: str | None = None
        self._token_expires_at = 0.0
        self._token_lock = threading.Lock()
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

    def search(self, query: str) -> dict[str, Any]:
        response = self._http.get(f"{self._base}/api/search", params={"query": query})
        response.raise_for_status()
        data = response.json()
        return dict(data) if isinstance(data, dict) else {}

    @staticmethod
    def _error(response: httpx.Response) -> str:
        try:
            data = response.json()
            if isinstance(data, dict) and data.get("error"):
                return str(data["error"])
        except ValueError:
            pass
        return f"HTTP {response.status_code}"

    def verify_account(self) -> bool:
        if not self._account_key:
            return False
        response = self._http.get(
            f"{self._api}/api/account/me",
            headers={"Authorization": f"Bearer {self._account_key}"},
        )
        if not response.is_success:
            return False
        data = response.json()
        return isinstance(data, dict) and bool(data.get("ok"))

    def _playback_token(self, *, refresh: bool = False) -> str:
        if not self._account_key:
            raise RuntimeError("Octave account key is required")
        with self._token_lock:
            if not refresh and self._token and time.monotonic() < self._token_expires_at:
                return self._token
            response = self._http.get(
                f"{self._api}/api/playback-token",
                headers={"Authorization": f"Bearer {self._account_key}"},
            )
            if not response.is_success:
                raise RuntimeError(f"Octave playback authorization failed: {self._error(response)}")
            data = response.json()
            token = str(data.get("token") or "")
            if not token:
                raise RuntimeError("Octave playback authorization returned no token")
            ttl = max(60, int(data.get("expiresIn") or 3600) - _TOKEN_SKEW_S)
            self._token = token
            self._token_expires_at = time.monotonic() + ttl
            return token

    def fetch(self, track_id: str, dest: Path) -> None:
        if not track_id.isdecimal():
            raise ValueError("invalid Octave track ID")
        for refresh in (False, True):
            token = self._playback_token(refresh=refresh)
            with self._http.stream(
                "GET",
                f"{self._api}/audio/lossless",
                params={"track": track_id, "dl": 1, "raw": 1, "k": token},
                timeout=120.0,
            ) as response:
                if response.status_code == _UNAUTHORIZED and not refresh:
                    continue
                if not response.is_success:
                    response.read()
                    raise RuntimeError(f"Octave download failed: {self._error(response)}")
                with dest.open("wb") as output:
                    for chunk in response.iter_bytes(_CHUNK):
                        output.write(chunk)
                return
        raise AssertionError("unreachable")
