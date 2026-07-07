"""Deezer client for the Tier-2 adapter.

Uses the user's ARL cookie (supplied via config) to mint a gw-light session,
resolve a track's TRACK_TOKEN, and get a FLAC CDN URL. The public search API
needs no auth. All network I/O; excluded from coverage — the crypto/parse logic
lives in the adapter.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx

_CHUNK = 64 * 1024  # 64 KiB — stream encrypted bytes to disk, never buffer the whole file
_GW = "https://www.deezer.com/ajax/gw-light.php"
_MEDIA = "https://media.deezer.com/v1/get_url"
_SEARCH = "https://api.deezer.com/search"


class DeezerClient:  # pragma: no cover - network I/O
    def __init__(self, arl: str) -> None:
        self._http = httpx.Client(
            cookies={"arl": arl},
            headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
            timeout=30.0,
        )
        self._api_token = ""
        self._license_token = ""
        self._authenticate()

    def _gw(self, method: str, api_token: str, payload: dict[str, Any]) -> dict[str, Any]:
        resp = self._http.post(
            _GW,
            params={"method": method, "input": "3", "api_version": "1.0", "api_token": api_token},
            json=payload,
        )
        resp.raise_for_status()
        return dict(resp.json().get("results", {}))

    def _authenticate(self) -> None:
        data = self._gw("deezer.getUserData", "", {})
        if not data.get("USER", {}).get("USER_ID"):
            raise RuntimeError("Deezer ARL invalid / not logged in")
        self._api_token = str(data["checkForm"])
        self._license_token = str(data["USER"]["OPTIONS"]["license_token"])

    def search(self, query: str) -> dict[str, Any]:
        resp = self._http.get(_SEARCH, params={"q": query, "limit": 10})
        resp.raise_for_status()
        return dict(resp.json())

    def resolve_url(self, track_id: int) -> str:
        song = self._gw("song.getData", self._api_token, {"sng_id": str(track_id)})
        token = song.get("TRACK_TOKEN")
        if not token:
            raise RuntimeError(f"no TRACK_TOKEN for {track_id}")
        resp = self._http.post(
            _MEDIA,
            json={
                "license_token": self._license_token,
                "media": [
                    {
                        "type": "FULL",
                        "formats": [{"cipher": "BF_CBC_STRIPE", "format": "FLAC"}],
                    }
                ],
                "track_tokens": [token],
            },
        )
        resp.raise_for_status()
        body = resp.json()
        try:
            return str(body["data"][0]["media"][0]["sources"][0]["url"])
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(f"no FLAC stream for {track_id} (account tier?): {body}") from exc

    def fetch(self, url: str, dest: Path) -> None:
        """Stream the encrypted FLAC at ``url`` to ``dest`` in 64 KiB chunks."""
        with self._http.stream("GET", url, timeout=120.0) as resp:
            resp.raise_for_status()
            with dest.open("wb") as fh:
                for chunk in resp.iter_bytes(_CHUNK):
                    fh.write(chunk)

    def close(self) -> None:
        self._http.close()
