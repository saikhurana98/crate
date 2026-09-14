"""Subsonic client: token auth, ping, search3, createPlaylist (fake transport)."""

from __future__ import annotations

import hashlib
from typing import Any

import pytest

from crate.infrastructure.navidrome.client import (
    NavidromeError,
    SubsonicClient,
    auth_params,
    start_scan,
)

OK = {"subsonic-response": {"status": "ok"}}


class _Post:
    def __init__(self, *replies: Any) -> None:
        self.replies = list(replies)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, url: str, params: Any) -> Any:
        self.calls.append((url, dict(params)))
        return self.replies.pop(0)


def _err(code: int, message: str = "nope") -> dict[str, Any]:
    return {"subsonic-response": {"status": "failed", "error": {"code": code, "message": message}}}


def test_token_auth_never_sends_the_password() -> None:
    p = auth_params("sai", "hunter2")
    assert "hunter2" not in p.values() and p["u"] == "sai" and p["f"] == "json"
    assert p["t"] == hashlib.md5(("hunter2" + p["s"]).encode()).hexdigest()


def test_ping_and_errors() -> None:
    post = _Post(OK, _err(40, "Wrong username or password"), _err(50), {})
    c = SubsonicClient("http://nd:4533/", "sai", "pw", post=post)
    c.ping()
    assert post.calls[0][0] == "http://nd:4533/rest/ping"
    with pytest.raises(NavidromeError, match="Wrong username") as bad:
        c.ping()
    assert bad.value.bad_credentials and bad.value.code == 40
    with pytest.raises(NavidromeError) as other:
        c.ping()
    assert not other.value.bad_credentials
    with pytest.raises(NavidromeError, match="refused") as empty:
        c.ping()
    assert empty.value.code is None


def test_search_songs() -> None:
    songs = [{"id": "1", "title": "T"}]
    post = _Post({"subsonic-response": {"status": "ok", "searchResult3": {"song": songs}}}, OK)
    c = SubsonicClient("http://nd", "sai", "pw", post=post)
    assert c.search_songs("Daft Punk Get Lucky") == songs
    url, params = post.calls[0]
    assert url.endswith("/rest/search3") and params["query"] == "Daft Punk Get Lucky"
    assert params["artistCount"] == "0" and params["songCount"] == "20"
    assert c.search_songs("x") == []


def test_save_playlist_creates_then_replaces() -> None:
    created = {"subsonic-response": {"status": "ok", "playlist": {"id": "pl1"}}}
    post = _Post(created, OK, {"subsonic-response": {"status": "ok", "playlist": {}}})
    c = SubsonicClient("http://nd", "sai", "pw", post=post)
    assert c.save_playlist("Mine", ["a", "b"]) == "pl1"
    assert post.calls[0][1]["name"] == "Mine" and post.calls[0][1]["songId"] == ["a", "b"]
    assert c.save_playlist("Mine", ["b"], "pl1") == "pl1"  # older servers reply without it
    assert post.calls[1][1]["playlistId"] == "pl1" and "name" not in post.calls[1][1]
    with pytest.raises(NavidromeError, match="id"):
        c.save_playlist("Mine", [])


def test_start_scan() -> None:
    seen: list[str] = []

    def get(url: str, params: dict[str, str]) -> Any:
        seen.append(url)
        return OK

    assert start_scan("http://nd/", "admin", "pw", get=get) is True
    assert seen == ["http://nd/rest/startScan"]
    assert start_scan("http://nd", "admin", "pw", get=lambda u, p: None) is False
