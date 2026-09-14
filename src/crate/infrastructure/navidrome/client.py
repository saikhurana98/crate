"""Navidrome over the Subsonic API.

- ``start_scan``: the admin rescan after a purge or restore, so the change shows
  up in the player without waiting for Navidrome's own schedule.
- :class:`SubsonicClient`: a user's own account, used to mirror imported
  playlists (ping, search3, createPlaylist).

Token auth throughout: ``t = md5(password + salt)``; the password itself is never
sent."""

from __future__ import annotations

import hashlib
import secrets
from collections.abc import Callable, Iterable, Mapping
from typing import Any

GetJson = Callable[[str, dict[str, str]], Any]
Params = Mapping[str, "str | list[str]"]
# POST the form ``params`` (a list value repeats the key) to the URL; return the JSON.
PostJson = Callable[[str, Params], Any]

API_VERSION = "1.16.1"
ERR_BAD_CREDENTIALS = 40
ERR_TOKEN_AUTH_UNSUPPORTED = 41  # e.g. an LDAP user: Subsonic token auth can't work
ERR_NOT_FOUND = 70


class NavidromeError(RuntimeError):
    """Navidrome answered with a Subsonic error (``code`` from its reply)."""

    def __init__(self, message: str, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code

    @property
    def bad_credentials(self) -> bool:
        return self.code in (ERR_BAD_CREDENTIALS, ERR_TOKEN_AUTH_UNSUPPORTED)


def auth_params(user: str, password: str) -> dict[str, str]:
    salt = secrets.token_hex(6)
    token = hashlib.md5((password + salt).encode(), usedforsecurity=False).hexdigest()
    return {"u": user, "t": token, "s": salt, "v": API_VERSION, "c": "crate", "f": "json"}


def _reply(data: Any) -> dict[str, Any]:
    reply = (data or {}).get("subsonic-response") or {}
    if reply.get("status") != "ok":
        err = reply.get("error") or {}
        code = err.get("code")
        raise NavidromeError(
            str(err.get("message") or "Navidrome refused the request"),
            int(code) if code is not None else None,
        )
    return dict(reply)


def start_scan(base_url: str, user: str, password: str, *, get: GetJson) -> bool:
    """Ask Navidrome to rescan its library; True when it accepted."""
    data = get(f"{base_url.rstrip('/')}/rest/startScan", auth_params(user, password))
    reply = (data or {}).get("subsonic-response") or {}
    return reply.get("status") == "ok"


class SubsonicClient:
    """One user's Navidrome account."""

    def __init__(self, base_url: str, user: str, password: str, *, post: PostJson) -> None:
        self._base = base_url.rstrip("/")
        self._user = user
        self._password = password
        self._post = post

    def _call(self, endpoint: str, params: Params | None = None) -> dict[str, Any]:
        form: dict[str, str | list[str]] = {**auth_params(self._user, self._password)}
        form.update(params or {})
        return _reply(self._post(f"{self._base}/rest/{endpoint}", form))

    def ping(self) -> None:
        """Raises :class:`NavidromeError` unless the credentials work."""
        self._call("ping")

    def search_songs(self, query: str, count: int = 20) -> list[dict[str, Any]]:
        reply = self._call(
            "search3",
            {"query": query, "songCount": str(count), "artistCount": "0", "albumCount": "0"},
        )
        return list((reply.get("searchResult3") or {}).get("song") or [])

    def save_playlist(
        self, name: str, song_ids: Iterable[str], playlist_id: str | None = None
    ) -> str:
        """Create a playlist, or replace every song in ``playlist_id``, in order.
        Returns the playlist's id."""
        params: dict[str, str | list[str]] = {"songId": list(song_ids)}
        if playlist_id:
            params["playlistId"] = playlist_id
        else:
            params["name"] = name
        reply = self._call("createPlaylist", params)
        pid = (reply.get("playlist") or {}).get("id") or playlist_id
        if not pid:
            raise NavidromeError("Navidrome didn't return the playlist's id")
        return str(pid)
