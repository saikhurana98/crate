"""Spotify Web API search client (console track search).

Client-credentials flow: exchange ``client_id:client_secret`` for a bearer
token (cached until ~30 s before expiry) and hit ``/v1/search`` for tracks.
Search-only — Spotify is never a download source, just how the console finds a
track to hand to the cascade.

I/O is injected (``post_form`` / ``get_json``) so the search normaliser is unit
tested without a network; the composition root supplies httpx-backed callables
over one pooled client for snappy repeated searches.
"""

from __future__ import annotations

import base64
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urlencode

TOKEN_URL = "https://accounts.spotify.com/api/token"
AUTHORIZE_URL = "https://accounts.spotify.com/authorize"
SEARCH_URL = "https://api.spotify.com/v1/search"
NOW_PLAYING_URL = "https://api.spotify.com/v1/me/player/currently-playing"
RECENT_URL = "https://api.spotify.com/v1/me/player/recently-played"

# user-scoped read of playback — the client-credentials flow cannot reach these.
USER_SCOPES = "user-read-currently-playing user-read-recently-played"

PostForm = Callable[[str, dict[str, str], dict[str, str]], Any]
GetJson = Callable[[str, dict[str, Any], dict[str, str]], Any]

_TOKEN_SKEW_S = 30.0
_MIN_ART = 64


def _best_art(images: list[dict[str, Any]]) -> str | None:
    """Smallest cover ≥64 px (thumbnail-friendly); else the smallest available."""
    if not images:
        return None
    usable = [i for i in images if int(i.get("height") or 0) >= _MIN_ART]
    pick = min(usable or images, key=lambda i: int(i.get("height") or 10_000))
    url = pick.get("url")
    return str(url) if url else None


def _track(item: dict[str, Any]) -> dict[str, Any]:
    artists = ", ".join(a["name"] for a in item.get("artists", []) if a.get("name"))
    album = item.get("album") or {}
    title = item.get("name", "")
    return {
        "id": item.get("id", ""),
        "title": title,
        "artist": artists,
        "album": album.get("name", ""),
        "art": _best_art(album.get("images", [])),
        "duration_ms": int(item.get("duration_ms") or 0),
        "year": str(album.get("release_date") or "")[:4],
        "url": (item.get("external_urls") or {}).get("spotify"),
        # ready-to-cascade free-text query (artist first disambiguates covers)
        "query": f"{artists} {title}".strip(),
    }


class SpotifyClient:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        *,
        post_form: PostForm,
        get_json: GetJson,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self._id = client_id
        self._secret = client_secret
        self._post_form = post_form
        self._get_json = get_json
        self._now = now
        self._token: str | None = None
        self._expires_at = 0.0

    def _bearer(self) -> str:
        if self._token is not None and self._now() < self._expires_at:
            return self._token
        basic = base64.b64encode(f"{self._id}:{self._secret}".encode()).decode()
        data = self._post_form(
            TOKEN_URL,
            {"grant_type": "client_credentials"},
            {"Authorization": f"Basic {basic}"},
        )
        self._token = str(data["access_token"])
        self._expires_at = self._now() + float(data.get("expires_in", 3600)) - _TOKEN_SKEW_S
        return self._token

    def search(self, query: str, limit: int = 20) -> list[dict[str, Any]]:
        data = self._get_json(
            SEARCH_URL,
            {"q": query, "type": "track", "limit": limit},
            {"Authorization": f"Bearer {self._bearer()}"},
        )
        items = ((data or {}).get("tracks") or {}).get("items") or []
        return [_track(it) for it in items]

    # --- user-scoped OAuth (authorization-code flow, for the Listening panel) ---
    def _basic(self) -> str:
        return base64.b64encode(f"{self._id}:{self._secret}".encode()).decode()

    def authorize_url(self, redirect_uri: str, state: str) -> str:
        """The Spotify consent URL the operator's browser is sent to on Connect."""
        return f"{AUTHORIZE_URL}?" + urlencode(
            {
                "client_id": self._id,
                "response_type": "code",
                "redirect_uri": redirect_uri,
                "scope": USER_SCOPES,
                "state": state,
            }
        )

    def exchange_code(self, code: str, redirect_uri: str) -> str:
        """Swap the callback ``code`` for a long-lived refresh token."""
        data = self._post_form(
            TOKEN_URL,
            {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri},
            {"Authorization": f"Basic {self._basic()}"},
        )
        return str(data["refresh_token"])

    def refresh_access_token(self, refresh_token: str) -> str:
        data = self._post_form(
            TOKEN_URL,
            {"grant_type": "refresh_token", "refresh_token": refresh_token},
            {"Authorization": f"Basic {self._basic()}"},
        )
        return str(data["access_token"])

    def currently_playing(self, access_token: str) -> dict[str, Any] | None:
        """The track playing now, or ``None`` (nothing playing / 204 empty body)."""
        data = self._get_json(NOW_PLAYING_URL, {}, {"Authorization": f"Bearer {access_token}"})
        item = (data or {}).get("item")
        if not item:
            return None
        track = _track(item)
        track["progress_ms"] = int((data or {}).get("progress_ms") or 0)
        track["is_playing"] = bool((data or {}).get("is_playing"))
        return track

    def recently_played(self, access_token: str, limit: int = 8) -> list[dict[str, Any]]:
        data = self._get_json(
            RECENT_URL, {"limit": limit}, {"Authorization": f"Bearer {access_token}"}
        )
        out: list[dict[str, Any]] = []
        for entry in (data or {}).get("items") or []:
            item = entry.get("track")
            if not item:
                continue
            track = _track(item)
            track["played_at"] = entry.get("played_at")
            out.append(track)
        return out
