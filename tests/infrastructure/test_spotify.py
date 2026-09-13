"""SpotifyClient — token caching + search normalisation (console search).

I/O is injected, so these exercise the token/expiry logic and the track mapper
without any network."""

from typing import Any

from crate.infrastructure.spotify.client import (
    LOGIN_SCOPES,
    RefreshedTokens,
    SpotifyClient,
    missing_scopes,
)

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
    assert tokens == {"access_token": "acc", "refresh_token": "ref", "scope": ""}


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
        "items": [
            {
                "id": "p1",
                "name": "Focus",
                "tracks": {"total": 1},
                "snapshot_id": "SNAP",
                "owner": {"id": "spotify"},
            },
            {"id": "p2", "name": "Mine", "tracks": {"total": 1}},
        ],
        "next": None,
    }
    pls = _oauth_client(get=lambda *_: page).current_user_playlists("AC")
    assert pls[0]["snapshot_id"] == "SNAP" and pls[0]["owner_id"] == "spotify"
    assert pls[1]["snapshot_id"] is None and pls[1]["owner_id"] is None


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


# --- PR 1: scopes, token rotation, 429 back-off, Library Sync reads ---
class _Resp:
    def __init__(self, status: int, headers: dict[str, str] | None = None) -> None:
        self.status_code = status
        self.headers = headers or {}


class _StatusError(Exception):
    """httpx.HTTPStatusError-shaped: carries ``.response.status_code/headers``."""

    def __init__(self, status: int, headers: dict[str, str] | None = None) -> None:
        super().__init__(f"HTTP {status}")
        self.response = _Resp(status, headers)


def _cc_post(url: str, data: dict[str, str], headers: dict[str, str]) -> Any:
    return {"access_token": "cc", "expires_in": 3600}


def _sleepy_client(get: Any) -> tuple[SpotifyClient, list[float]]:
    sleeps: list[float] = []
    client = SpotifyClient("cid", "csec", post_form=_cc_post, get_json=get, sleep=sleeps.append)
    return client, sleeps


def test_login_scopes_include_top_read_and_missing_scopes() -> None:
    assert "user-top-read" in LOGIN_SCOPES.split()
    assert missing_scopes(None) == ["user-top-read"]  # legacy token, no scope recorded
    assert missing_scopes(LOGIN_SCOPES) == []
    assert "user-top-read" in missing_scopes("user-read-email")


def test_exchange_code_tokens_captures_granted_scope() -> None:
    post = lambda *_: {"access_token": "a", "refresh_token": "r", "scope": "x y"}  # noqa: E731
    assert _oauth_client(post=post).exchange_code_tokens("c", "https://cb")["scope"] == "x y"
    bare = lambda *_: {"access_token": "a", "refresh_token": "r"}  # noqa: E731
    assert _oauth_client(post=bare).exchange_code_tokens("c", "https://cb")["scope"] == ""


def test_refresh_tokens_keeps_rotation_and_scope() -> None:
    rotated = lambda *_: {"access_token": "a2", "refresh_token": "r2", "scope": "s"}  # noqa: E731
    client = _oauth_client(post=rotated)
    assert client.refresh_tokens("r1") == RefreshedTokens("a2", "r2", "s")
    assert client.refresh_access_token("r1") == "a2"  # back-compat wrapper
    plain = lambda *_: {"access_token": "a3"}  # noqa: E731
    assert _oauth_client(post=plain).refresh_tokens("r1") == RefreshedTokens("a3", None, None)


def test_get_retries_429_honouring_retry_after() -> None:
    calls = {"n": 0}

    def get(url: str, params: dict[str, Any], headers: dict[str, str]) -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            raise _StatusError(429, {"Retry-After": "2"})
        return {"id": "me1", "display_name": "Sai"}

    client, sleeps = _sleepy_client(get)
    assert client.me("tok")["id"] == "me1"
    assert sleeps == [2.0] and calls["n"] == 2


def test_get_429_without_header_defaults_to_one_second() -> None:
    calls = {"n": 0}

    def get(url: str, params: dict[str, Any], headers: dict[str, str]) -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            raise _StatusError(429)
        return {"id": "me1"}

    client, sleeps = _sleepy_client(get)
    client.me("tok")
    assert sleeps == [1.0]


def test_get_gives_up_when_retry_after_exceeds_cap() -> None:
    def get(*_: Any) -> Any:
        raise _StatusError(429, {"Retry-After": "120"})

    client, sleeps = _sleepy_client(get)
    try:
        client.me("tok")
        raise AssertionError("expected the 429 to surface")
    except _StatusError:
        pass
    assert sleeps == []  # never parks a sync thread for two minutes


def test_get_429_retries_are_bounded() -> None:
    def get(*_: Any) -> Any:
        raise _StatusError(429, {"Retry-After": "1"})

    client, sleeps = _sleepy_client(get)
    try:
        client.me("tok")
        raise AssertionError("expected the 429 to surface")
    except _StatusError:
        pass
    assert sleeps == [1.0, 1.0]  # 3 attempts, 2 back-offs, then raise


def test_non_429_errors_propagate_immediately() -> None:
    def get(*_: Any) -> Any:
        raise _StatusError(500)

    client, sleeps = _sleepy_client(get)
    try:
        client.me("tok")
        raise AssertionError("expected a 500 to surface")
    except _StatusError:
        pass
    assert sleeps == []


def _sp_track(i: int) -> dict[str, Any]:
    return {
        "id": f"t{i}",
        "uri": f"spotify:track:t{i}",
        "name": f"Song {i}",
        "artists": [{"name": "Artist"}],
        "album": {"name": "Album", "images": []},
        "duration_ms": 200_000,
        "external_ids": {"isrc": f"ISRC{i}"},
    }


def test_top_tracks_pages_by_offset_and_time_range() -> None:
    seen: list[dict[str, Any]] = []

    def get(url: str, params: dict[str, Any], headers: dict[str, str]) -> Any:
        seen.append(dict(params))
        if params["offset"] == 0:
            return {"items": [_sp_track(i) for i in range(50)], "next": "more"}
        return {"items": [_sp_track(i) for i in range(50, 70)], "next": None}

    tracks = _oauth_client(get=get).top_tracks("tok", "long_term")
    assert len(tracks) == 70 and tracks[0]["isrc"] == "ISRC0"
    assert [p["offset"] for p in seen] == [0, 50]
    assert all(p["time_range"] == "long_term" for p in seen)


def test_top_tracks_respects_limit() -> None:
    get = lambda *_: {"items": [_sp_track(i) for i in range(50)], "next": "more"}  # noqa: E731
    assert len(_oauth_client(get=get).top_tracks("tok", "short_term", limit=30)) == 30


def test_recently_played_after_orders_oldest_first_and_returns_cursor() -> None:
    seen: dict[str, Any] = {}

    def get(url: str, params: dict[str, Any], headers: dict[str, str]) -> Any:
        seen.update(params)
        return {
            "items": [
                {"played_at": "2026-09-01T10:05:00Z", "track": _sp_track(2)},
                {"played_at": "2026-09-01T10:00:00Z", "track": _sp_track(1)},
                {"played_at": None, "track": _sp_track(3)},  # unusable -> dropped
            ],
            "cursors": {"after": "1788000300000"},
        }

    plays, cursor = _oauth_client(get=get).recently_played_after("tok", 1788000000000)
    assert seen["after"] == 1788000000000 and seen["limit"] == 50
    assert [p["id"] for p in plays] == ["t1", "t2"]  # oldest first
    assert plays[0]["uri"] == "spotify:track:t1" and plays[0]["played_at"].endswith("Z")
    assert cursor == 1788000300000


def test_recently_played_after_keeps_cursor_when_nothing_new() -> None:
    get = lambda *_: {"items": [], "cursors": None}  # noqa: E731
    plays, cursor = _oauth_client(get=get).recently_played_after("tok", 42)
    assert plays == [] and cursor == 42


def test_tracks_batch_chunks_by_fifty() -> None:
    calls: list[str] = []

    def get(url: str, params: dict[str, Any], headers: dict[str, str]) -> Any:
        ids = params["ids"].split(",")
        calls.append(params["ids"])
        return {"tracks": [_sp_track(int(i[1:])) for i in ids]}

    client = SpotifyClient("cid", "csec", post_form=_cc_post, get_json=get)
    tracks = client.tracks_batch([f"t{i}" for i in range(120)] + [""])
    assert len(calls) == 3 and len(tracks) == 120  # 50 + 50 + 20; blank id dropped
    assert tracks[119]["isrc"] == "ISRC119"


def test_tracks_batch_falls_back_to_single_gets_on_403() -> None:
    singles: list[str] = []

    def get(url: str, params: dict[str, Any], headers: dict[str, str]) -> Any:
        if "ids" in params:
            raise _StatusError(403)  # several-tracks refused for this app
        singles.append(url.rsplit("/", 1)[-1])
        return _sp_track(int(url.rsplit("/t", 1)[-1]))

    client = SpotifyClient("cid", "csec", post_form=_cc_post, get_json=get)
    tracks = client.tracks_batch(["t1", "t2"])
    assert singles == ["t1", "t2"] and [t["id"] for t in tracks] == ["t1", "t2"]


def test_tracks_batch_reraises_other_errors() -> None:
    def get(*_: Any) -> Any:
        raise _StatusError(500)

    client = SpotifyClient("cid", "csec", post_form=_cc_post, get_json=get)
    try:
        client.tracks_batch(["t1"])
        raise AssertionError("expected a 500 to surface")
    except _StatusError:
        pass


def test_album_tracks_follows_pagination() -> None:
    def get(url: str, params: dict[str, Any], headers: dict[str, str]) -> Any:
        if url.endswith("/albums/AL1"):
            return {
                "name": "LP",
                "artists": [{"name": "Band"}],
                "tracks": {"items": [_sp_track(1), {"name": None}], "next": "page2"},
            }
        return {"items": [_sp_track(2)], "next": None}

    client = SpotifyClient("cid", "csec", post_form=_cc_post, get_json=get)
    album = client.album_tracks("AL1")
    assert album["name"] == "LP" and album["artist"] == "Band"
    assert [t["title"] for t in album["tracks"]] == ["Song 1", "Song 2"]  # nameless dropped
