"""Subsonic client: token auth, ping, search3, createPlaylist (fake transport)."""

from __future__ import annotations

import hashlib
import json
from typing import Any

import pytest

from crate.domain.navidrome import NavidromeLogin
from crate.infrastructure.navidrome.client import (
    NavidromeError,
    SubsonicClient,
    auth_params,
    parse_app_config,
    sso_login,
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


def _page(config: dict[str, Any]) -> str:
    # what Navidrome serves: the JSON config inside a JS string literal
    return f"<script>window.__APP_CONFIG__ = {json.dumps(json.dumps(config))}</script>"


def test_token_login_sends_the_stored_pair() -> None:
    post = _Post(OK)
    c = SubsonicClient.for_login(
        "http://nd", NavidromeLogin("sai", token="tk", salt="sl"), post=post
    )
    c.ping()
    params = post.calls[0][1]
    assert (params["u"], params["t"], params["s"]) == ("sai", "tk", "sl")


def test_parse_app_config() -> None:
    assert parse_app_config(_page({"baseURL": "", "auth": {"username": "a"}}))["auth"] == {
        "username": "a"
    }
    with pytest.raises(NavidromeError, match="doesn't look like"):
        parse_app_config("<html>nope</html>")
    with pytest.raises(NavidromeError, match="couldn't read"):
        parse_app_config('window.__APP_CONFIG__ = "{not json"')
    with pytest.raises(NavidromeError, match="couldn't read"):
        parse_app_config('window.__APP_CONFIG__ = "[1]"')


def test_sso_login_reads_the_token_navidrome_issued() -> None:
    seen: list[tuple[str, dict[str, str]]] = []
    auth = {"username": "saik", "subsonicToken": "t0k", "subsonicSalt": "s4lt", "token": "jwt"}

    def get(url: str, headers: dict[str, str]) -> str:
        seen.append((url, headers))
        return _page({"baseURL": "", "auth": auth})

    login = sso_login("http://nd:4533/", "Remote-User", "saik", get=get)
    assert login == NavidromeLogin("saik", token="t0k", salt="s4lt")
    assert seen == [("http://nd:4533/app/", {"Remote-User": "saik"})]
    with pytest.raises(NavidromeError, match="WHITELIST"):
        sso_login("http://nd", "Remote-User", "saik", get=lambda u, h: _page({"baseURL": ""}))
