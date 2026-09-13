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
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

TOKEN_URL = "https://accounts.spotify.com/api/token"
AUTHORIZE_URL = "https://accounts.spotify.com/authorize"
SEARCH_URL = "https://api.spotify.com/v1/search"
NOW_PLAYING_URL = "https://api.spotify.com/v1/me/player/currently-playing"
RECENT_URL = "https://api.spotify.com/v1/me/player/recently-played"
PLAYLIST_URL = "https://api.spotify.com/v1/playlists/"
ME_URL = "https://api.spotify.com/v1/me"
SAVED_TRACKS_URL = "https://api.spotify.com/v1/me/tracks"
MY_PLAYLISTS_URL = "https://api.spotify.com/v1/me/playlists"
TOP_TRACKS_URL = "https://api.spotify.com/v1/me/top/tracks"
TRACKS_URL = "https://api.spotify.com/v1/tracks"
ALBUM_URL = "https://api.spotify.com/v1/albums/"

_PLAYLIST_ID = re.compile(r"playlist[/:]([A-Za-z0-9]+)")

# user-scoped read of playback — the client-credentials flow cannot reach these.
USER_SCOPES = "user-read-currently-playing user-read-recently-played"
# Login-with-Spotify needs identity (/v1/me) + private library reads (Phase B
# import). A stored token with no recorded scope was minted under this set.
LEGACY_LOGIN_SCOPES = (
    "user-read-email user-library-read "
    "playlist-read-private playlist-read-collaborative " + USER_SCOPES
)
# Library Sync tiers add ``user-top-read`` (top tracks per time range). Adding a
# scope forces a one-time re-consent; ``missing_scopes`` drives the UI banner.
LOGIN_SCOPES = LEGACY_LOGIN_SCOPES + " user-top-read"

PostForm = Callable[[str, dict[str, str], dict[str, str]], Any]
GetJson = Callable[[str, dict[str, Any], dict[str, str]], Any]

_TOKEN_SKEW_S = 30.0
_MIN_ART = 64
_MAX_429_RETRIES = 2  # bounded back-off: never hang a sync on a rate limit
_MAX_RETRY_AFTER_S = 30.0  # a longer Retry-After raises instead of sleeping
_BATCH = 50  # Spotify's page / several-ids cap
_TOP_TRACKS_MAX = 100  # a top-tracks time range serves ~100 at most


def missing_scopes(granted: str | None) -> list[str]:
    """Scopes current features need that a user's token lacks (sorted). ``None``
    — a token minted before scopes were recorded — is read as the legacy login
    set, so those users are prompted to re-login once."""
    have = set((granted if granted is not None else LEGACY_LOGIN_SCOPES).split())
    return sorted(set(LOGIN_SCOPES.split()) - have)


def _status_of(exc: BaseException) -> int | None:
    """HTTP status carried by a transport error (httpx.HTTPStatusError shape),
    duck-typed so this client stays transport-agnostic."""
    code = getattr(getattr(exc, "response", None), "status_code", None)
    return code if isinstance(code, int) else None


def _retry_after_s(exc: BaseException) -> float | None:
    """Seconds to back off for a 429 (``Retry-After``, default 1 s); ``None`` when
    the error isn't a rate limit."""
    if _status_of(exc) != 429:  # noqa: PLR2004 - HTTP status
        return None
    headers = getattr(getattr(exc, "response", None), "headers", None)
    raw = headers.get("Retry-After") if headers is not None else None
    try:
        return max(0.0, float(raw)) if raw is not None else 1.0
    except (TypeError, ValueError):
        return 1.0


@dataclass(frozen=True, slots=True)
class RefreshedTokens:
    """A user-token refresh. Spotify may **rotate** the refresh token and reports
    the granted ``scope``; callers persist both (a dropped rotated token eventually
    breaks the user's session)."""

    access_token: str
    refresh_token: str | None
    scope: str | None


def _best_art(images: list[dict[str, Any]]) -> str | None:
    """Smallest cover ≥64 px (thumbnail-friendly); else the smallest available."""
    if not images:
        return None
    usable = [i for i in images if int(i.get("height") or 0) >= _MIN_ART]
    pick = min(usable or images, key=lambda i: int(i.get("height") or 10_000))
    url = pick.get("url")
    return str(url) if url else None


def _min_track(track: dict[str, Any]) -> dict[str, Any] | None:
    """Minimal cascade-ready dict from a Spotify track object, or ``None`` when it
    has no title (local/unavailable rows). Shared by playlist / Liked-Songs /
    user-playlist reads so they all match on ISRC + duration."""
    title = track.get("name")
    if not title:
        return None
    artists = ", ".join(a["name"] for a in track.get("artists", []) if a.get("name"))
    duration_ms = int(track.get("duration_ms") or 0)
    isrc = (track.get("external_ids") or {}).get("isrc")
    return {
        "query": f"{artists} {title}".strip(),
        "artist": artists or None,
        "title": str(title),
        "isrc": str(isrc) if isrc else None,
        "duration_s": round(duration_ms / 1000) if duration_ms else None,
    }


def _split_next(url: str) -> tuple[str, dict[str, str]]:
    """A ``next`` link as ``(base, params)``. Transports like httpx *replace* a
    URL's query string with an explicit ``params`` dict, even an empty one, so
    following ``next`` with ``params={}`` silently refetched page 1."""
    parts = urlsplit(url)
    return urlunsplit(parts._replace(query="")), dict(parse_qsl(parts.query))


def _track(item: dict[str, Any]) -> dict[str, Any]:
    artists = ", ".join(a["name"] for a in item.get("artists", []) if a.get("name"))
    album = item.get("album") or {}
    title = item.get("name", "")
    duration_ms = int(item.get("duration_ms") or 0)
    isrc = (item.get("external_ids") or {}).get("isrc")
    return {
        "id": item.get("id", ""),
        "title": title,
        "artist": artists,
        "album": album.get("name", ""),
        "art": _best_art(album.get("images", [])),
        "duration_ms": duration_ms,
        # rounded seconds for the cascade's duration match; None when unknown
        "duration_s": round(duration_ms / 1000) if duration_ms else None,
        "isrc": str(isrc) if isrc else None,
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
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._id = client_id
        self._secret = client_secret
        self._post_form = post_form
        self._get_json = get_json
        self._now = now
        self._sleep = sleep
        self._token: str | None = None
        self._expires_at = 0.0

    def _get(self, url: str, params: dict[str, Any], headers: dict[str, str]) -> Any:
        """``get_json`` with a bounded 429 back-off: honour ``Retry-After`` (up to
        ``_MAX_RETRY_AFTER_S``) at most ``_MAX_429_RETRIES`` times, then raise."""
        for attempt in range(_MAX_429_RETRIES + 1):
            try:
                return self._get_json(url, params, headers)
            except Exception as exc:
                wait = _retry_after_s(exc)
                if wait is None or wait > _MAX_RETRY_AFTER_S or attempt == _MAX_429_RETRIES:
                    raise
                self._sleep(wait)
        raise AssertionError("unreachable")  # pragma: no cover

    def _next_page(self, nxt: Any, headers: dict[str, str]) -> Any:
        """Fetch a ``next`` pagination link, keeping its offset/limit."""
        base, params = _split_next(str(nxt))
        return self._get(base, params, headers)

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
        data = self._get(
            SEARCH_URL,
            {"q": query, "type": "track", "limit": limit},
            {"Authorization": f"Bearer {self._bearer()}"},
        )
        items = ((data or {}).get("tracks") or {}).get("items") or []
        return [_track(it) for it in items]

    def playlist(self, ref: str, max_tracks: int = 500) -> dict[str, Any]:
        """Resolve a playlist (id, URL, or spotify: URI) to
        ``{name, owner, tracks: [track]}`` via client-credentials (public
        playlists). Each track is a dict with ``query`` (artist-first free text)
        plus the structured identity fields (``artist``, ``title``, ``isrc``,
        ``duration_s``) so bulk imports match on ISRC/duration too. Follows
        pagination up to ``max_tracks``."""
        m = _PLAYLIST_ID.search(ref)
        pid = m.group(1) if m else ref.strip()
        headers = {"Authorization": f"Bearer {self._bearer()}"}
        data = self._get(
            f"{PLAYLIST_URL}{pid}",
            {
                "fields": "name,owner(display_name),tracks(items(track(name,artists(name),"
                "duration_ms,external_ids)),next)"
            },
            headers,
        )
        name = str((data or {}).get("name") or "Playlist")
        owner = str(((data or {}).get("owner") or {}).get("display_name") or "")
        tracks_obj = (data or {}).get("tracks") or {}
        tracks: list[dict[str, Any]] = []

        def collect(items: list[dict[str, Any]]) -> None:
            for entry in items or []:
                track = _min_track((entry or {}).get("track") or {})
                if track is not None:
                    tracks.append(track)

        collect(tracks_obj.get("items") or [])
        nxt = tracks_obj.get("next")
        while nxt and len(tracks) < max_tracks:
            page = self._next_page(nxt, headers)
            collect((page or {}).get("items") or [])
            nxt = (page or {}).get("next")
        return {"name": name, "owner": owner, "tracks": tracks[:max_tracks]}

    # --- user-scoped OAuth (authorization-code flow, for the Listening panel) ---
    def _basic(self) -> str:
        return base64.b64encode(f"{self._id}:{self._secret}".encode()).decode()

    def authorize_url(self, redirect_uri: str, state: str, scope: str = USER_SCOPES) -> str:
        """The Spotify consent URL the browser is sent to. ``scope`` defaults to
        the player scopes; the login flow passes :data:`LOGIN_SCOPES`."""
        return f"{AUTHORIZE_URL}?" + urlencode(
            {
                "client_id": self._id,
                "response_type": "code",
                "redirect_uri": redirect_uri,
                "scope": scope,
                "state": state,
            }
        )

    def exchange_code_tokens(self, code: str, redirect_uri: str) -> dict[str, str]:
        """Swap the callback ``code`` for both tokens: ``{access_token,
        refresh_token}``. Login needs the access token immediately to read
        ``/v1/me``; the refresh token is persisted per user."""
        data = self._post_form(
            TOKEN_URL,
            {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri},
            {"Authorization": f"Basic {self._basic()}"},
        )
        return {
            "access_token": str(data["access_token"]),
            "refresh_token": str(data["refresh_token"]),
            "scope": str(data.get("scope") or ""),  # what the user actually granted
        }

    def exchange_code(self, code: str, redirect_uri: str) -> str:
        """Swap the callback ``code`` for a long-lived refresh token."""
        return self.exchange_code_tokens(code, redirect_uri)["refresh_token"]

    def me(self, access_token: str) -> dict[str, Any]:
        """The authenticated user's profile from ``GET /v1/me``:
        ``{id, display_name, email, avatar}``."""
        data = self._get(ME_URL, {}, {"Authorization": f"Bearer {access_token}"}) or {}
        return {
            "id": str(data.get("id") or ""),
            "display_name": str(data.get("display_name") or data.get("id") or ""),
            "email": (str(data["email"]) if data.get("email") else None),
            "avatar": _best_art(data.get("images") or []),
        }

    def refresh_tokens(self, refresh_token: str) -> RefreshedTokens:
        """Refresh a user token, keeping any rotated refresh token + granted scope."""
        data = self._post_form(
            TOKEN_URL,
            {"grant_type": "refresh_token", "refresh_token": refresh_token},
            {"Authorization": f"Basic {self._basic()}"},
        )
        return RefreshedTokens(
            access_token=str(data["access_token"]),
            refresh_token=str(data["refresh_token"]) if data.get("refresh_token") else None,
            scope=str(data["scope"]) if data.get("scope") else None,
        )

    def refresh_access_token(self, refresh_token: str) -> str:
        return self.refresh_tokens(refresh_token).access_token

    def currently_playing(self, access_token: str) -> dict[str, Any] | None:
        """The track playing now, or ``None`` (nothing playing / 204 empty body)."""
        data = self._get(NOW_PLAYING_URL, {}, {"Authorization": f"Bearer {access_token}"})
        item = (data or {}).get("item")
        if not item:
            return None
        track = _track(item)
        track["progress_ms"] = int((data or {}).get("progress_ms") or 0)
        track["is_playing"] = bool((data or {}).get("is_playing"))
        return track

    def recently_played(self, access_token: str, limit: int = 8) -> list[dict[str, Any]]:
        data = self._get(RECENT_URL, {"limit": limit}, {"Authorization": f"Bearer {access_token}"})
        out: list[dict[str, Any]] = []
        for entry in (data or {}).get("items") or []:
            item = entry.get("track")
            if not item:
                continue
            track = _track(item)
            track["played_at"] = entry.get("played_at")
            out.append(track)
        return out

    # --- user library (private reads, need the per-user access token) ---
    def _paged_tracks(
        self, url: str, access_token: str, params: dict[str, Any], max_tracks: int
    ) -> list[dict[str, Any]]:
        """Follow ``next`` pagination collecting ``_min_track`` dicts from items
        shaped ``{track: {...}}`` (Liked Songs and playlist-track endpoints)."""
        headers = {"Authorization": f"Bearer {access_token}"}
        out: list[dict[str, Any]] = []
        data = self._get(url, {"limit": 50, **params}, headers)
        while data:
            for entry in (data or {}).get("items") or []:
                track = _min_track((entry or {}).get("track") or {})
                if track is not None:
                    out.append(track)
                if len(out) >= max_tracks:
                    return out
            nxt = (data or {}).get("next")
            data = self._next_page(nxt, headers) if nxt else None
        return out

    def saved_tracks(self, access_token: str, max_tracks: int = 2000) -> list[dict[str, Any]]:
        """The user's Liked Songs (``GET /v1/me/tracks``)."""
        return self._paged_tracks(SAVED_TRACKS_URL, access_token, {}, max_tracks)

    def user_playlist_tracks(
        self, access_token: str, playlist_id: str, max_tracks: int = 2000
    ) -> list[dict[str, Any]]:
        """Tracks of one of the user's own playlists (works for private ones)."""
        url = f"{PLAYLIST_URL}{playlist_id}/tracks"
        return self._paged_tracks(url, access_token, {}, max_tracks)

    def user_playlist_snapshot(self, access_token: str, playlist_id: str) -> str | None:
        """The playlist's current ``snapshot_id`` — a cheap ``fields=snapshot_id``
        fetch the sync watcher uses to skip unchanged playlists without pulling the
        whole track list. ``None`` if the playlist is gone / the field is absent."""
        data = self._get(
            f"{PLAYLIST_URL}{playlist_id}",
            {"fields": "snapshot_id"},
            {"Authorization": f"Bearer {access_token}"},
        )
        snap = (data or {}).get("snapshot_id")
        return str(snap) if snap else None

    def current_user_playlists(
        self, access_token: str, max_playlists: int = 200
    ) -> list[dict[str, Any]]:
        """The user's playlists (``GET /v1/me/playlists``) as
        ``{id, name, count, image}`` dicts."""
        headers = {"Authorization": f"Bearer {access_token}"}
        out: list[dict[str, Any]] = []
        data = self._get(MY_PLAYLISTS_URL, {"limit": 50}, headers)
        while data:
            for pl in (data or {}).get("items") or []:
                if not pl or not pl.get("id"):
                    continue
                out.append(
                    {
                        "id": str(pl["id"]),
                        "name": str(pl.get("name") or "Playlist"),
                        "count": int(((pl.get("tracks") or {}).get("total")) or 0),
                        "image": _best_art(pl.get("images") or []),
                        "snapshot_id": (str(pl["snapshot_id"]) if pl.get("snapshot_id") else None),
                        # owner "spotify" = a Spotify-made playlist (Discover Weekly, mixes)
                        "owner_id": (str((pl.get("owner") or {}).get("id") or "") or None),
                    }
                )
                if len(out) >= max_playlists:
                    return out
            nxt = (data or {}).get("next")
            data = self._next_page(nxt, headers) if nxt else None
        return out

    # --- Library Sync reads (top tracks / play ledger / metadata / albums) ---
    def top_tracks(
        self, access_token: str, time_range: str, limit: int = _TOP_TRACKS_MAX
    ) -> list[dict[str, Any]]:
        """The user's top tracks for ``short_term`` (~4 weeks), ``medium_term``
        (~6 months) or ``long_term`` (~1 year), best first. Needs ``user-top-read``."""
        headers = {"Authorization": f"Bearer {access_token}"}
        out: list[dict[str, Any]] = []
        while len(out) < limit:
            page = (
                self._get(
                    TOP_TRACKS_URL,
                    {
                        "time_range": time_range,
                        "limit": min(_BATCH, limit - len(out)),
                        "offset": len(out),
                    },
                    headers,
                )
                or {}
            )
            items = [it for it in page.get("items") or [] if it]
            out.extend(_track(it) for it in items)
            if not items or not page.get("next"):
                break
        return out[:limit]

    def recently_played_after(
        self, access_token: str, after_ms: int | None = None
    ) -> tuple[list[dict[str, Any]], int | None]:
        """Plays newer than ``after_ms`` (epoch ms), oldest first, plus the cursor to
        pass next time. Spotify keeps only the last 50 plays, so the ledger polls
        often enough that none fall off between calls."""
        params: dict[str, Any] = {"limit": _BATCH}
        if after_ms is not None:
            params["after"] = after_ms
        data = self._get(RECENT_URL, params, {"Authorization": f"Bearer {access_token}"}) or {}
        plays: list[dict[str, Any]] = []
        for entry in data.get("items") or []:
            item = (entry or {}).get("track")
            if not item or not entry.get("played_at"):
                continue
            track = _track(item)
            track["played_at"] = str(entry["played_at"])
            track["uri"] = item.get("uri") or (
                f"spotify:track:{track['id']}" if track["id"] else None
            )
            plays.append(track)
        plays.sort(key=lambda p: p["played_at"])
        cursor = (data.get("cursors") or {}).get("after")
        return plays, (int(cursor) if cursor else after_ms)

    def tracks_batch(self, ids: list[str]) -> list[dict[str, Any]]:
        """Full track objects (ISRC, art, duration) for ``ids`` via client
        credentials, 50 per call. Falls back to one call per id when the
        several-tracks endpoint is refused (403/404 — restricted dev-mode apps)."""
        headers = {"Authorization": f"Bearer {self._bearer()}"}
        out: list[dict[str, Any]] = []
        clean = [i for i in ids if i]
        for start in range(0, len(clean), _BATCH):
            chunk = clean[start : start + _BATCH]
            try:
                data = self._get(TRACKS_URL, {"ids": ",".join(chunk)}, headers) or {}
                out.extend(_track(t) for t in data.get("tracks") or [] if t)
            except Exception as exc:
                if _status_of(exc) not in (403, 404):
                    raise
                for tid in chunk:
                    single = self._get(f"{TRACKS_URL}/{tid}", {}, headers)
                    if single:
                        out.append(_track(single))
        return out

    def album_tracks(self, album_id: str, max_tracks: int = 200) -> dict[str, Any]:
        """An album as ``{name, artist, tracks}`` (client credentials) — for the
        "queue whole album" suggestion. Tracks are cascade-ready ``_min_track`` dicts."""
        headers = {"Authorization": f"Bearer {self._bearer()}"}
        data = self._get(f"{ALBUM_URL}{album_id}", {}, headers) or {}
        artist = ", ".join(a["name"] for a in data.get("artists") or [] if a.get("name"))
        tracks: list[dict[str, Any]] = []
        page: dict[str, Any] | None = data.get("tracks") or {}
        while page and len(tracks) < max_tracks:
            for item in page.get("items") or []:
                track = _min_track(item or {})
                if track is not None:
                    tracks.append(track)
            nxt = page.get("next")
            page = self._next_page(nxt, headers) if nxt else None
        return {
            "name": str(data.get("name") or "Album"),
            "artist": artist,
            "tracks": tracks[:max_tracks],
        }
