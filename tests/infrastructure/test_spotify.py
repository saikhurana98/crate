"""SpotifyClient — token caching + search normalisation (console search).

I/O is injected, so these exercise the token/expiry logic and the track mapper
without any network."""

from typing import Any

from crate.infrastructure.spotify.client import SpotifyClient

_SEARCH_PAYLOAD: dict[str, Any] = {
    "tracks": {
        "items": [
            {
                "id": "t1",
                "name": "Get Lucky",
                "duration_ms": 248_000,
                "external_ids": {"isrc": "USQX91300108"},
                "artists": [{"name": "Daft Punk"}, {"name": "Pharrell Williams"}],
                "album": {
                    "name": "Random Access Memories",
                    "release_date": "2013-05-17",
                    "images": [
                        {"url": "big", "height": 640},
                        {"url": "mid", "height": 300},
                        {"url": "tiny", "height": 64},
                    ],
                },
                "external_urls": {"spotify": "https://open.spotify.com/track/t1"},
            }
        ]
    }
}


def _client(clock: list[float] | None = None) -> tuple[SpotifyClient, dict[str, int]]:
    calls = {"token": 0, "search": 0}
    ticks = clock or [0.0]

    def post_form(url: str, data: dict[str, str], headers: dict[str, str]) -> Any:
        calls["token"] += 1
        assert data["grant_type"] == "client_credentials"
        assert headers["Authorization"].startswith("Basic ")
        return {"access_token": f"tok{calls['token']}", "expires_in": 3600}

    def get_json(url: str, params: dict[str, Any], headers: dict[str, str]) -> Any:
        calls["search"] += 1
        assert headers["Authorization"].startswith("Bearer tok")
        assert params["type"] == "track"
        return _SEARCH_PAYLOAD

    seq = list(ticks)
    i = [0]

    def clock_fn() -> float:
        # advance through the scripted ticks, holding the last value at the end
        val = seq[min(i[0], len(seq) - 1)]
        i[0] += 1
        return val

    client = SpotifyClient("id", "secret", post_form=post_form, get_json=get_json, now=clock_fn)
    return client, calls


def test_search_normalises_track() -> None:
    client, _ = _client()
    [track] = client.search("get lucky")
    assert track["id"] == "t1"
    assert track["title"] == "Get Lucky"
    assert track["artist"] == "Daft Punk, Pharrell Williams"
    assert track["album"] == "Random Access Memories"
    assert track["year"] == "2013"
    assert track["duration_ms"] == 248_000
    assert track["url"] == "https://open.spotify.com/track/t1"
    # artist-first query disambiguates covers
    assert track["query"] == "Daft Punk, Pharrell Williams Get Lucky"
    # smallest cover >= 64px chosen for a snappy thumbnail
    assert track["art"] == "tiny"
    # Phase 1: ISRC + rounded duration parsed for the cascade's identity match
    assert track["isrc"] == "USQX91300108"
    assert track["duration_s"] == 248


def test_search_isrc_and_duration_default_to_none_when_absent() -> None:
    payload: dict[str, Any] = {
        "tracks": {"items": [{"id": "x", "name": "N", "artists": [], "duration_ms": 0}]}
    }

    def post_form(url: str, data: dict[str, str], headers: dict[str, str]) -> Any:
        return {"access_token": "t", "expires_in": 3600}

    def get_json(url: str, params: dict[str, Any], headers: dict[str, str]) -> Any:
        return payload

    client = SpotifyClient("i", "s", post_form=post_form, get_json=get_json)
    [track] = client.search("x")
    assert track["isrc"] is None and track["duration_s"] is None


def test_token_is_cached_across_searches() -> None:
    client, calls = _client(clock=[0.0, 1.0, 2.0])
    client.search("a")
    client.search("b")
    assert calls["token"] == 1  # one token fetch, reused
    assert calls["search"] == 2


def test_token_refreshes_after_expiry() -> None:
    # first token minted at t=0 (expires ~3570); the next search checks the clock
    # and finds it past expiry, forcing a re-mint.
    client, calls = _client(clock=[0.0, 5000.0, 5000.0])
    client.search("a")
    client.search("b")
    assert calls["token"] == 2


def test_missing_art_and_fields_are_safe() -> None:
    empty: dict[str, Any] = {"tracks": {"items": [{"id": "x", "name": "N", "artists": []}]}}

    def post_form(url: str, data: dict[str, str], headers: dict[str, str]) -> Any:
        return {"access_token": "t", "expires_in": 3600}

    def get_json(url: str, params: dict[str, Any], headers: dict[str, str]) -> Any:
        return empty

    client = SpotifyClient("i", "s", post_form=post_form, get_json=get_json)
    [track] = client.search("x")
    assert track["art"] is None and track["album"] == "" and track["query"] == "N"


def test_empty_payload_returns_no_tracks() -> None:
    def post_form(url: str, data: dict[str, str], headers: dict[str, str]) -> Any:
        return {"access_token": "t", "expires_in": 3600}

    def get_json(url: str, params: dict[str, Any], headers: dict[str, str]) -> Any:
        return {}

    client = SpotifyClient("i", "s", post_form=post_form, get_json=get_json)
    assert client.search("nope") == []


# --- user-scoped OAuth + player (the Listening panel) ---

_ITEM: dict[str, Any] = {
    "id": "sp15",
    "name": "Loud Places",
    "duration_ms": 283_000,
    "artists": [{"name": "Jamie xx"}, {"name": "Romy"}],
    "album": {"name": "In Colour", "release_date": "2015", "images": []},
    "external_urls": {"spotify": "u"},
}


def _oauth_client(post: Any = None, get: Any = None) -> SpotifyClient:
    def post_form(url: str, data: dict[str, str], headers: dict[str, str]) -> Any:
        return post(url, data, headers) if post else {}

    def get_json(url: str, params: dict[str, Any], headers: dict[str, str]) -> Any:
        return get(url, params, headers) if get else None

    return SpotifyClient("cid", "csec", post_form=post_form, get_json=get_json)


def test_authorize_url_has_scopes_and_state() -> None:
    url = _oauth_client().authorize_url("https://c/cb", "st8")
    assert url.startswith("https://accounts.spotify.com/authorize?")
    assert "client_id=cid" in url
    assert "response_type=code" in url
    assert "user-read-currently-playing" in url and "user-read-recently-played" in url
    assert "redirect_uri=https%3A%2F%2Fc%2Fcb" in url
    assert "state=st8" in url


def test_exchange_code_returns_refresh_token() -> None:
    seen: dict[str, Any] = {}

    def post(url: str, data: dict[str, str], headers: dict[str, str]) -> Any:
        seen.update(data=data, headers=headers)
        return {"access_token": "a", "refresh_token": "r3fr3sh", "expires_in": 3600}

    rt = _oauth_client(post=post).exchange_code("thecode", "https://c/cb")
    assert rt == "r3fr3sh"
    assert seen["data"] == {
        "grant_type": "authorization_code",
        "code": "thecode",
        "redirect_uri": "https://c/cb",
    }
    assert seen["headers"]["Authorization"].startswith("Basic ")


def test_refresh_access_token() -> None:
    def post(url: str, data: dict[str, str], headers: dict[str, str]) -> Any:
        assert data == {"grant_type": "refresh_token", "refresh_token": "rt"}
        return {"access_token": "fresh", "expires_in": 3600}

    assert _oauth_client(post=post).refresh_access_token("rt") == "fresh"


def test_login_scope_authorize_url() -> None:
    from crate.infrastructure.spotify.client import LOGIN_SCOPES

    url = _oauth_client().authorize_url("https://c/cb", "st8", LOGIN_SCOPES)
    assert "user-read-email" in url
    assert "user-library-read" in url
    assert "playlist-read-private" in url
    assert "state=st8" in url


def test_exchange_code_tokens_returns_both() -> None:
    def post(url: str, data: dict[str, str], headers: dict[str, str]) -> Any:
        return {"access_token": "acc", "refresh_token": "ref", "expires_in": 3600}

    tokens = _oauth_client(post=post).exchange_code_tokens("code", "https://c/cb")
    assert tokens == {"access_token": "acc", "refresh_token": "ref"}


def test_me_shapes_profile() -> None:
    def get(url: str, params: dict[str, Any], headers: dict[str, str]) -> Any:
        assert url.endswith("/v1/me")
        assert headers["Authorization"] == "Bearer AC"
        return {
            "id": "spot123",
            "display_name": "Sai",
            "email": "sai@example.com",
            "images": [{"url": "http://img/big", "height": 300}],
        }

    me = _oauth_client(get=get).me("AC")
    assert me["id"] == "spot123"
    assert me["display_name"] == "Sai"
    assert me["email"] == "sai@example.com"
    assert me["avatar"] == "http://img/big"


def test_me_falls_back_to_id_and_handles_missing_fields() -> None:
    me = _oauth_client(get=lambda *_: {"id": "onlyid"}).me("AC")
    assert me["display_name"] == "onlyid"
    assert me["email"] is None and me["avatar"] is None


def _track_obj(name: str) -> dict[str, Any]:
    return {"name": name, "artists": [{"name": "A"}], "duration_ms": 200_000, "external_ids": {}}


def test_saved_tracks_paginates() -> None:
    pages = {
        "https://api.spotify.com/v1/me/tracks": {
            "items": [{"track": _track_obj("One")}],
            "next": "https://api.spotify.com/v1/me/tracks?offset=50",
        },
        "https://api.spotify.com/v1/me/tracks?offset=50": {
            "items": [{"track": _track_obj("Two")}, {"track": {"name": ""}}],  # blank skipped
            "next": None,
        },
    }

    def get(url: str, params: dict[str, Any], headers: dict[str, str]) -> Any:
        assert headers["Authorization"] == "Bearer AC"
        return pages[url]

    tracks = _oauth_client(get=get).saved_tracks("AC")
    assert [t["title"] for t in tracks] == ["One", "Two"]


def test_saved_tracks_respects_max() -> None:
    page = {"items": [{"track": _track_obj(f"T{i}")} for i in range(50)], "next": "n"}
    tracks = _oauth_client(get=lambda *_: page).saved_tracks("AC", max_tracks=10)
    assert len(tracks) == 10


def test_user_playlist_tracks_uses_playlist_endpoint() -> None:
    seen: dict[str, str] = {}

    def get(url: str, params: dict[str, Any], headers: dict[str, str]) -> Any:
        seen["url"] = url
        return {"items": [{"track": _track_obj("Song")}], "next": None}

    tracks = _oauth_client(get=get).user_playlist_tracks("AC", "PID")
    assert tracks[0]["title"] == "Song"
    assert seen["url"] == "https://api.spotify.com/v1/playlists/PID/tracks"


def test_current_user_playlists_shapes_and_paginates() -> None:
    pages = {
        "https://api.spotify.com/v1/me/playlists": {
            "items": [
                {"id": "p1", "name": "Focus", "tracks": {"total": 12}, "images": []},
                {"id": None, "name": "skip"},  # no id -> skipped
            ],
            "next": "https://api.spotify.com/v1/me/playlists?offset=50",
        },
        "https://api.spotify.com/v1/me/playlists?offset=50": {
            "items": [{"id": "p2", "name": "Chill", "tracks": {"total": 5}, "images": []}],
            "next": None,
        },
    }
    pls = _oauth_client(get=lambda url, *_: pages[url]).current_user_playlists("AC")
    assert [(p["id"], p["name"], p["count"]) for p in pls] == [
        ("p1", "Focus", 12),
        ("p2", "Chill", 5),
    ]


def test_current_user_playlists_includes_snapshot() -> None:
    page = {
        "items": [{"id": "p1", "name": "Focus", "tracks": {"total": 1}, "snapshot_id": "SNAP"}],
        "next": None,
    }
    pls = _oauth_client(get=lambda *_: page).current_user_playlists("AC")
    assert pls[0]["snapshot_id"] == "SNAP"


def test_user_playlist_snapshot_fetches_field() -> None:
    seen: dict[str, Any] = {}

    def get(url: str, params: dict[str, Any], headers: dict[str, str]) -> Any:
        seen["url"], seen["params"] = url, params
        return {"snapshot_id": "SNAP7"}

    snap = _oauth_client(get=get).user_playlist_snapshot("AC", "PID")
    assert snap == "SNAP7"
    assert seen["url"] == "https://api.spotify.com/v1/playlists/PID"
    assert seen["params"] == {"fields": "snapshot_id"}


def test_user_playlist_snapshot_none_when_absent() -> None:
    assert _oauth_client(get=lambda *_: {}).user_playlist_snapshot("AC", "PID") is None


def test_currently_playing_shapes_track() -> None:
    def get(url: str, params: dict[str, Any], headers: dict[str, str]) -> Any:
        assert url.endswith("/currently-playing")
        assert headers["Authorization"] == "Bearer AC"
        return {"is_playing": True, "progress_ms": 42_000, "item": _ITEM}

    np = _oauth_client(get=get).currently_playing("AC")
    assert np is not None
    assert np["title"] == "Loud Places"
    assert np["artist"] == "Jamie xx, Romy"
    assert np["progress_ms"] == 42_000
    assert np["is_playing"] is True
    assert np["query"] == "Jamie xx, Romy Loud Places"


def test_currently_playing_none_when_idle() -> None:
    # 204 -> injected get_json returns None; also no "item" -> None
    assert _oauth_client(get=lambda *_: None).currently_playing("AC") is None
    assert _oauth_client(get=lambda *_: {"item": None}).currently_playing("AC") is None


def test_recently_played_shapes_and_skips_empty() -> None:
    def get(url: str, params: dict[str, Any], headers: dict[str, str]) -> Any:
        assert url.endswith("/recently-played")
        assert params["limit"] == 8
        return {
            "items": [
                {"track": _ITEM, "played_at": "2026-07-05T18:00:00Z"},
                {"track": None, "played_at": "x"},  # skipped
            ]
        }

    recent = _oauth_client(get=get).recently_played("AC", limit=8)
    assert len(recent) == 1
    assert recent[0]["title"] == "Loud Places"
    assert recent[0]["played_at"] == "2026-07-05T18:00:00Z"


def test_playlist_resolves_tracks_with_pagination() -> None:
    page1 = {
        "name": "Focus",
        "owner": {"display_name": "sai"},
        "tracks": {
            "items": [
                {
                    "track": {
                        "name": "One",
                        "artists": [{"name": "A"}],
                        "duration_ms": 210_000,
                        "external_ids": {"isrc": "US1234567890"},
                    }
                },
                {"track": None},  # skipped
            ],
            "next": "https://api.spotify.com/v1/playlists/x/tracks?offset=100",
        },
    }
    page2 = {
        "items": [{"track": {"name": "Two", "artists": [{"name": "B"}, {"name": "C"}]}}],
        "next": None,
    }

    calls = {"n": 0}

    def get(url: str, params: dict[str, Any], headers: dict[str, str]) -> Any:
        calls["n"] += 1
        assert headers["Authorization"].startswith("Bearer ")
        return page1 if calls["n"] == 1 else page2

    def post(url: str, data: dict[str, str], headers: dict[str, str]) -> Any:
        return {"access_token": "t", "expires_in": 3600}

    pl = _oauth_client(post=post, get=get).playlist("https://open.spotify.com/playlist/abc123?si=z")
    assert pl["name"] == "Focus" and pl["owner"] == "sai"
    # tracks are structured identity dicts (artist-first query, ISRC + duration)
    assert [t["query"] for t in pl["tracks"]] == ["A One", "B, C Two"]  # second page followed
    assert pl["tracks"][0] == {
        "query": "A One",
        "artist": "A",
        "title": "One",
        "isrc": "US1234567890",
        "duration_s": 210,
    }
    assert pl["tracks"][1]["isrc"] is None and pl["tracks"][1]["duration_s"] is None
