"""Console ops API (FastAPI, read-only): routes, filters, auth, 404 (ADR-0018).

Drives the app with a real RequestStore over a temp DB via FastAPI's TestClient."""

from pathlib import Path

from fastapi.testclient import TestClient

from crate.adapters.inbound.ops_api import create_app
from crate.application.download import DownloadReport
from crate.domain.audio import AudioInfo, QualityVerdict
from crate.domain.download import DownloadOutcome
from crate.domain.identity import TrackIdentity, from_query
from crate.infrastructure.crypto import SecretBox
from crate.infrastructure.sqlite.config_store import ConfigStore
from crate.infrastructure.sqlite.db import connect
from crate.infrastructure.sqlite.store import RequestStore


def _config(tmp_path: Path) -> ConfigStore:
    return ConfigStore(connect(tmp_path / "cfg.db"), SecretBox(bytes(range(32))))


def _report(source: str = "qobuz", tier: int = 1) -> DownloadReport:
    outcome = DownloadOutcome(Path(f"/nas/{source}.flac"), tier, source, 3.0, "q")
    info = AudioInfo("flac", "flac", 96000, 2, 24, 1_800_000, 200.0, 90_000_000)
    verdict = QualityVerdict("lossless 24-bit / 96 kHz flac", True)
    return DownloadReport("q", outcome, info, verdict, None)


def _seeded_store(tmp_path: Path) -> RequestStore:
    store = RequestStore(connect(tmp_path / "crate.db"), requested_via="explo")
    done = store.open_request("Daft Punk Get Lucky")
    store.record_attempt(done, "monochrome", 1, False, "Turnstile JWT required")
    store.record_attempt(done, "qobuz", 1, True, "lossless 24-bit / 96 kHz flac")
    store.finish_request(done, _report())
    store.open_request("Fleetwood Mac Dreams")  # in-flight
    return store


def test_feed_shows_everyone_with_both_latencies(tmp_path: Path) -> None:
    client = TestClient(create_app(_seeded_store(tmp_path)))
    rows = client.get("/api/feed").json()
    assert isinstance(rows, list) and rows
    done = next(r for r in rows if r["status"] == "done")
    # who requested it + both latencies present on a completed lossless grab
    assert "user_name" in done and "user_id" in done
    assert done["ttf_playable_s"] is not None
    assert done["ttf_final_s"] is not None
    # the still-in-flight row is in the feed too, latencies unset
    inflight = next(r for r in rows if r["status"] == "downloading")
    assert inflight["ttf_playable_s"] is None and inflight["ttf_final_s"] is None


def test_feed_limit_is_bounded(tmp_path: Path) -> None:
    client = TestClient(create_app(_seeded_store(tmp_path)))
    assert client.get("/api/feed", params={"limit": 0}).status_code == 422
    assert client.get("/api/feed", params={"limit": 500}).status_code == 422


def test_playlist_sync_subscribe_list_and_remove(tmp_path: Path) -> None:
    client = TestClient(create_app(_seeded_store(tmp_path)))
    assert client.get("/api/library/sync").json() == []  # nothing synced yet
    r = client.post("/api/library/sync", json={"playlist_id": "p1", "name": "Road Trip"})
    assert r.status_code == 200 and r.json()["active"] is True
    rows = client.get("/api/library/sync").json()
    assert len(rows) == 1 and rows[0]["spotify_ref"] == "p1" and rows[0]["active"] is True
    # unsubscribe -> the row stays (ledger preserved) but goes inactive
    assert client.delete("/api/library/sync/p1").json()["active"] is False
    assert client.get("/api/library/sync").json()[0]["active"] is False


def test_playlist_sync_validation_and_404(tmp_path: Path) -> None:
    client = TestClient(create_app(_seeded_store(tmp_path)))
    assert client.post("/api/library/sync", json={"name": "x"}).status_code == 400
    assert client.delete("/api/library/sync/never-synced").status_code == 404


def test_health_needs_no_key(tmp_path: Path) -> None:
    client = TestClient(create_app(_seeded_store(tmp_path), api_key="secret"))
    r = client.get("/api/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"


def test_stats_and_requests(tmp_path: Path) -> None:
    client = TestClient(create_app(_seeded_store(tmp_path)))
    stats = client.get("/api/stats").json()
    assert stats["total"] == 2 and stats["done"] == 1 and stats["in_flight"] == 1
    assert stats["wins_by_source"] == {"qobuz": 1}

    all_reqs = client.get("/api/requests").json()
    assert len(all_reqs) == 2
    inflight = client.get("/api/requests", params={"status": "downloading"}).json()
    assert len(inflight) == 1 and inflight[0]["query"] == "Fleetwood Mac Dreams"
    hit = client.get("/api/requests", params={"q": "daft"}).json()
    assert len(hit) == 1 and hit[0]["source_won"] == "qobuz"


def test_request_detail_and_404(tmp_path: Path) -> None:
    store = _seeded_store(tmp_path)
    client = TestClient(create_app(store))
    rid = client.get("/api/requests", params={"status": "done"}).json()[0]["id"]
    detail = client.get(f"/api/requests/{rid}").json()
    assert len(detail["attempts"]) == 2
    assert detail["download"]["bit_depth"] == 24
    assert client.get("/api/requests/nope").status_code == 404


def test_sources_endpoint_uses_injected_provider(tmp_path: Path) -> None:
    fake = [{"name": "qobuz", "tier": 1, "enabled": True, "credentials": "ok"}]
    client = TestClient(create_app(_seeded_store(tmp_path), sources=lambda: fake))
    assert client.get("/api/sources").json() == fake


def test_search_uses_injected_provider_and_caches(tmp_path: Path) -> None:
    calls = {"n": 0}

    def fake_search(q: str, limit: int) -> list[dict]:
        calls["n"] += 1
        return [{"id": "t1", "title": "Get Lucky", "query": q, "limit": limit}]

    client = TestClient(create_app(_seeded_store(tmp_path), search=fake_search))
    r = client.get("/api/search", params={"q": "get lucky"})
    assert r.status_code == 200 and r.json()[0]["title"] == "Get Lucky"
    assert r.json()[0]["limit"] == 20  # default
    client.get("/api/search", params={"q": "get lucky"})  # identical -> cached
    assert calls["n"] == 1


def test_search_503_when_not_configured(tmp_path: Path) -> None:
    client = TestClient(create_app(_seeded_store(tmp_path)))
    assert client.get("/api/search", params={"q": "x"}).status_code == 503


def test_search_requires_query(tmp_path: Path) -> None:
    client = TestClient(create_app(_seeded_store(tmp_path), search=lambda q, n: []))
    assert client.get("/api/search").status_code == 422  # q missing
    assert client.get("/api/search", params={"q": ""}).status_code == 422  # min_length


def test_download_enqueues_a_queued_request(tmp_path: Path) -> None:
    store = _seeded_store(tmp_path)
    client = TestClient(create_app(store))
    r = client.post("/api/download", json={"query": "Daft Punk One More Time"})
    assert r.status_code == 200 and r.json()["status"] == "queued"
    rid = r.json()["id"]
    queued = client.get("/api/requests", params={"status": "queued"}).json()
    assert [x["id"] for x in queued] == [rid]
    claimed = store.claim_next()
    assert claimed is not None
    assert claimed["id"] == rid and claimed["query"] == "Daft Punk One More Time"


def test_download_requires_nonempty_query(tmp_path: Path) -> None:
    client = TestClient(create_app(_seeded_store(tmp_path)))
    assert client.post("/api/download", json={"query": "   "}).status_code == 400
    assert client.post("/api/download", json={}).status_code == 400


def test_download_threads_identity_from_payload(tmp_path: Path) -> None:
    store = _seeded_store(tmp_path)
    client = TestClient(create_app(store))
    r = client.post(
        "/api/download",
        json={
            "query": "Daft Punk Get Lucky",
            "artist": "Daft Punk",
            "title": "Get Lucky",
            "isrc": "USQX91300108",
            "duration_s": 248,
        },
    )
    assert r.status_code == 200
    claimed = store.claim_next()
    assert claimed is not None
    assert claimed["identity"] == TrackIdentity("Daft Punk", "Get Lucky", "USQX91300108", 248)


def test_download_without_identity_fields_stays_plain_query(tmp_path: Path) -> None:
    store = _seeded_store(tmp_path)
    client = TestClient(create_app(store))
    client.post("/api/download", json={"query": "just a query"})
    claimed = store.claim_next()
    assert claimed is not None and claimed["identity"] == TrackIdentity()


def test_download_accepts_duration_alias_and_bad_duration(tmp_path: Path) -> None:
    store = _seeded_store(tmp_path)
    client = TestClient(create_app(store))
    # "duration" alias works; a non-numeric duration is ignored, not an error
    client.post("/api/download", json={"query": "q", "duration": 200})
    client.post("/api/download", json={"query": "q2", "title": "T", "duration_s": "oops"})
    first = store.claim_next()
    second = store.claim_next()
    assert first is not None and first["identity"].duration_s == 200
    assert second is not None and second["identity"] == TrackIdentity(title="T")


def test_api_key_enforced(tmp_path: Path) -> None:
    client = TestClient(create_app(_seeded_store(tmp_path), api_key="secret"))
    assert client.get("/api/stats").status_code == 401
    assert client.get("/api/stats", headers={"X-API-Key": "wrong"}).status_code == 401
    assert client.get("/api/stats", headers={"X-API-Key": "secret"}).status_code == 200


# --- Login with Spotify (Phase 2) ---
from crate.application.auth import User  # noqa: E402


class _FakeAuth:
    """Minimal AuthPort for the API tests. login_url embeds the state so the
    callback can round-trip it; complete_login always mints ``sess-raw``."""

    RAW = "sess-raw"

    def __init__(self) -> None:
        self.user = User(id="u1", spotify_id="spot1", display_name="Sai", avatar_url="http://a")
        self._revoked: set[str] = set()

    def login_url(self, state: str) -> str:
        return f"https://accounts.spotify.com/authorize?state={state}"

    def complete_login(self, code: str) -> tuple[User, str]:
        if code == "bad":
            raise RuntimeError("exchange failed")
        return self.user, self.RAW

    def resolve_session(self, raw_token: str) -> User | None:
        if raw_token == self.RAW and raw_token not in self._revoked:
            return self.user
        return None

    def logout(self, raw_token: str) -> None:
        self._revoked.add(raw_token)


def _state_from(location: str) -> str:
    return location.split("state=", 1)[1]


def test_me_without_auth_returns_service_user(tmp_path: Path) -> None:
    client = TestClient(create_app(_seeded_store(tmp_path)))
    me = client.get("/api/me").json()
    assert me["id"] == "service" and me["display_name"] == "service"
    assert me["missing_scopes"] == []


def test_session_required_when_auth_configured(tmp_path: Path) -> None:
    auth = _FakeAuth()
    client = TestClient(create_app(_seeded_store(tmp_path), auth=auth))
    # no cookie -> 401 on the authed router
    assert client.get("/api/stats").status_code == 401
    # a valid session cookie -> 200 + /api/me is the real user
    cookies = {"crate_session": _FakeAuth.RAW}
    assert client.get("/api/stats", cookies=cookies).status_code == 200
    me = client.get("/api/me", cookies=cookies).json()
    assert me["display_name"] == "Sai" and me["spotify_id"] == "spot1"


def test_api_key_still_bypasses_session(tmp_path: Path) -> None:
    client = TestClient(create_app(_seeded_store(tmp_path), api_key="secret", auth=_FakeAuth()))
    assert client.get("/api/stats").status_code == 401
    assert client.get("/api/stats", headers={"X-API-Key": "secret"}).status_code == 200


def test_login_and_callback_sets_cookie(tmp_path: Path) -> None:
    client = TestClient(create_app(_seeded_store(tmp_path), auth=_FakeAuth()))
    login = client.get("/api/auth/login", follow_redirects=False)
    assert login.status_code == 307
    state = _state_from(login.headers["location"])

    cb = client.get(
        "/api/auth/callback", params={"code": "ok", "state": state}, follow_redirects=False
    )
    assert cb.status_code == 303 and cb.headers["location"] == "/"
    set_cookie = cb.headers["set-cookie"]
    assert "crate_session=sess-raw" in set_cookie
    assert "HttpOnly" in set_cookie and "Secure" in set_cookie
    assert "samesite=lax" in set_cookie.lower()


def test_callback_rejects_unknown_state(tmp_path: Path) -> None:
    client = TestClient(create_app(_seeded_store(tmp_path), auth=_FakeAuth()))
    cb = client.get(
        "/api/auth/callback", params={"code": "ok", "state": "forged"}, follow_redirects=False
    )
    assert cb.status_code == 303 and cb.headers["location"] == "/?login=error"
    assert "set-cookie" not in cb.headers


def test_callback_exchange_failure_redirects_error(tmp_path: Path) -> None:
    client = TestClient(create_app(_seeded_store(tmp_path), auth=_FakeAuth()))
    state = _state_from(client.get("/api/auth/login", follow_redirects=False).headers["location"])
    cb = client.get(
        "/api/auth/callback", params={"code": "bad", "state": state}, follow_redirects=False
    )
    assert cb.headers["location"] == "/?login=error"


def test_login_endpoints_503_without_auth(tmp_path: Path) -> None:
    client = TestClient(create_app(_seeded_store(tmp_path)))
    assert client.get("/api/auth/login", follow_redirects=False).status_code == 503
    assert client.get("/api/auth/callback", params={"code": "x", "state": "y"}).status_code == 503


def test_logout_revokes_and_clears_cookie(tmp_path: Path) -> None:
    auth = _FakeAuth()
    client = TestClient(create_app(_seeded_store(tmp_path), auth=auth))
    cookies = {"crate_session": _FakeAuth.RAW}
    out = client.post("/api/auth/logout", cookies=cookies, follow_redirects=False)
    assert out.status_code == 303
    assert 'crate_session=""' in out.headers["set-cookie"] or "crate_session=;" in out.headers[
        "set-cookie"
    ].replace(" ", "")
    # session now revoked -> authed access fails
    assert client.get("/api/stats", cookies=cookies).status_code == 401


def test_requests_limit_validation(tmp_path: Path) -> None:
    client = TestClient(create_app(_seeded_store(tmp_path)))
    assert client.get("/api/requests", params={"limit": 0}).status_code == 422
    assert client.get("/api/requests", params={"limit": 501}).status_code == 422


# --- config endpoints (Phase 2) ---


def test_config_get_seeds_then_put_bumps_version(tmp_path: Path) -> None:
    client = TestClient(create_app(_seeded_store(tmp_path), config=_config(tmp_path)))
    got = client.get("/api/config").json()
    assert "tiers" in got and got["version"] >= 1
    layout = {"tiers": got["tiers"], "disabled": got["disabled"]}
    layout["tiers"][0]["strategy"] = "uniform"
    r = client.put("/api/config", json=layout)
    assert r.status_code == 200 and r.json()["version"] > got["version"]
    assert client.get("/api/config").json()["tiers"][0]["strategy"] == "uniform"


def test_config_put_invalid_is_400(tmp_path: Path) -> None:
    client = TestClient(create_app(_seeded_store(tmp_path), config=_config(tmp_path)))
    assert client.put("/api/config", json={"tiers": [], "disabled": []}).status_code == 400


def test_config_unavailable_without_store_is_503(tmp_path: Path) -> None:
    client = TestClient(create_app(_seeded_store(tmp_path)))
    assert client.get("/api/config").status_code == 503


def test_put_source_stores_encrypted_credentials(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    client = TestClient(create_app(_seeded_store(tmp_path), config=cfg))
    client.get("/api/config")  # seed layout
    r = client.put(
        "/api/sources/qobuz",
        json={
            "credentials": {
                "CRATE_QOBUZ_APP_ID": "a",
                "CRATE_QOBUZ_APP_SECRET": "b",
                "CRATE_QOBUZ_TOKEN": "c",
            }
        },
    )
    assert r.status_code == 200 and r.json()["credentials"] == "ok"
    assert cfg.get_secret("qobuz", "CRATE_QOBUZ_TOKEN") == "c"


def test_put_source_enabled_toggle_moves_in_and_out(tmp_path: Path) -> None:
    client = TestClient(create_app(_seeded_store(tmp_path), config=_config(tmp_path)))
    client.get("/api/config")
    assert client.put("/api/sources/telegram", json={"enabled": True}).json()["enabled"] is True
    assert "telegram" not in client.get("/api/config").json()["disabled"]
    client.put("/api/sources/telegram", json={"enabled": False})
    assert "telegram" in client.get("/api/config").json()["disabled"]


def test_put_source_unknown_is_404(tmp_path: Path) -> None:
    client = TestClient(create_app(_seeded_store(tmp_path), config=_config(tmp_path)))
    assert client.put("/api/sources/nope", json={}).status_code == 404


def test_put_source_cannot_disable_pinned_ytdlp(tmp_path: Path) -> None:
    client = TestClient(create_app(_seeded_store(tmp_path), config=_config(tmp_path)))
    client.get("/api/config")
    assert client.put("/api/sources/ytdlp", json={"enabled": False}).status_code == 400


def test_sources_reflect_stored_layout(tmp_path: Path) -> None:
    client = TestClient(create_app(_seeded_store(tmp_path), config=_config(tmp_path)))
    client.get("/api/config")
    srcs = {s["name"]: s for s in client.get("/api/sources").json()}
    assert srcs["ytdlp"]["enabled"] is True  # pinned in tier 3


# --- Spotify "Listening" panel routes (per-user; token from login) ---


class _FakePort:
    def __init__(self, connected: bool = False) -> None:
        self._connected = connected
        self.seen: list[str] = []
        self.missing: list[str] = []
        self.boom = False

    def missing_scopes(self, user_id: str) -> list[str]:
        if self.boom:
            raise RuntimeError("vault down")
        return self.missing

    def connected(self, user_id: str) -> bool:
        return self._connected

    def player(self, user_id: str) -> dict[str, object]:
        self.seen.append(user_id)
        return {"now_playing": {"title": "X", "query": "a X"}, "recent": []}


def test_spotify_status_absent_vs_present(tmp_path: Path) -> None:
    off = TestClient(create_app(_seeded_store(tmp_path)))
    assert off.get("/api/spotify/status").json() == {"configured": False, "connected": False}
    on = TestClient(create_app(_seeded_store(tmp_path), spotify=_FakePort(connected=True)))
    assert on.get("/api/spotify/status").json() == {"configured": True, "connected": True}


def test_spotify_player_states(tmp_path: Path) -> None:
    # absent -> 409 (not connected)
    off = TestClient(create_app(_seeded_store(tmp_path)))
    assert off.get("/api/spotify/player").status_code == 409
    # configured but not connected -> 409
    disc = TestClient(create_app(_seeded_store(tmp_path), spotify=_FakePort(connected=False)))
    assert disc.get("/api/spotify/player").status_code == 409
    # connected -> payload, scoped to the current user (service in single-user mode)
    port = _FakePort(connected=True)
    on = TestClient(create_app(_seeded_store(tmp_path), spotify=port))
    body = on.get("/api/spotify/player").json()
    assert body["now_playing"]["title"] == "X" and body["recent"] == []
    assert port.seen == ["service"]


# --- Spotify library import (per-user) ---


class _FakeLibrary:
    def my_playlists(self, user_id: str) -> list[dict]:
        return [{"id": "p1", "name": "Focus", "count": 3, "image": None}]

    def liked_songs(self, user_id: str, max_tracks: int = 2000) -> list[dict]:
        return [{"query": "A T", "artist": "A", "title": "T", "isrc": "X", "duration_s": 200}]

    def playlist_tracks(self, user_id: str, playlist_id: str, max_tracks: int = 2000) -> list[dict]:
        return [{"query": f"pl {playlist_id}", "artist": "A", "title": playlist_id}]


def test_library_playlists(tmp_path: Path) -> None:
    client = TestClient(create_app(_seeded_store(tmp_path), library=_FakeLibrary()))
    pls = client.get("/api/library/playlists").json()
    assert pls[0]["name"] == "Focus" and pls[0]["count"] == 3


def test_library_playlists_503_when_absent(tmp_path: Path) -> None:
    client = TestClient(create_app(_seeded_store(tmp_path)))
    assert client.get("/api/library/playlists").status_code == 503


def test_library_import_liked_enqueues_batch(tmp_path: Path) -> None:
    store = _seeded_store(tmp_path)
    client = TestClient(create_app(store, library=_FakeLibrary()))
    r = client.post("/api/library/import", json={"source": "liked"})
    assert r.status_code == 200 and r.json()["total"] == 1 and r.json()["name"] == "Liked Songs"
    claimed = store.claim_next()
    assert claimed is not None and claimed["identity"].isrc == "X"


def test_library_import_playlist_requires_id(tmp_path: Path) -> None:
    client = TestClient(create_app(_seeded_store(tmp_path), library=_FakeLibrary()))
    assert client.post("/api/library/import", json={"source": "playlist"}).status_code == 400
    ok = client.post("/api/library/import", json={"source": "playlist", "playlist_id": "p1"})
    assert ok.status_code == 200 and ok.json()["total"] == 1


def test_library_import_bad_source_and_empty(tmp_path: Path) -> None:
    client = TestClient(create_app(_seeded_store(tmp_path), library=_FakeLibrary()))
    assert client.post("/api/library/import", json={"source": "nope"}).status_code == 400

    class _Empty(_FakeLibrary):
        def liked_songs(self, user_id: str, max_tracks: int = 2000) -> list[dict]:
            return []

    c2 = TestClient(create_app(_seeded_store(tmp_path), library=_Empty()))
    assert c2.post("/api/library/import", json={"source": "liked"}).status_code == 404


# --- bulk downloads ---
from crate.application.bulk import ResolvedBatch  # noqa: E402


def _ids(*queries: str) -> list[TrackIdentity]:
    return [from_query(q) for q in queries]


def _bulk_client(tmp_path: Path, resolver=None) -> TestClient:
    store = RequestStore(connect(tmp_path / "bulk.db"), requested_via="console")
    return TestClient(create_app(store, bulk_resolve=resolver))


def test_bulk_creates_batch_and_enqueues(tmp_path: Path) -> None:
    resolver = lambda text: ResolvedBatch("My PL", "paste", "", _ids("a x", "b y", "c z"))  # noqa: E731
    client = _bulk_client(tmp_path, resolver)
    r = client.post("/api/bulk", json={"text": "a x\nb y\nc z"})
    assert r.status_code == 200
    body = r.json()
    assert body["origin"] == "paste" and body["total"] == 3
    batches = client.get("/api/batches").json()
    assert len(batches) == 1 and batches[0]["counts"]["queued"] == 3
    detail = client.get(f"/api/batches/{body['id']}").json()
    assert len(detail["tracks"]) == 3


def test_bulk_threads_identity_into_enqueued_rows(tmp_path: Path) -> None:
    store = RequestStore(connect(tmp_path / "bulkid.db"), requested_via="console")
    batch = ResolvedBatch(
        "PL", "spotify", "sai", [TrackIdentity("Dua Lipa", "Levitating", "GBAHT2000123", 203)]
    )
    client = TestClient(create_app(store, bulk_resolve=lambda t: batch))
    r = client.post("/api/bulk", json={"text": "https://open.spotify.com/playlist/x"})
    assert r.status_code == 200 and r.json()["total"] == 1
    claimed = store.claim_next()
    assert claimed is not None
    assert claimed["query"] == "Dua Lipa Levitating"  # display() as the free-text query
    assert claimed["identity"] == TrackIdentity("Dua Lipa", "Levitating", "GBAHT2000123", 203)


def test_bulk_error_mapping(tmp_path: Path) -> None:
    # no resolver -> 503
    assert _bulk_client(tmp_path).post("/api/bulk", json={"text": "x"}).status_code == 503
    # empty text -> 400
    c = _bulk_client(tmp_path, lambda t: ResolvedBatch("n", "paste", "", _ids("a")))
    assert c.post("/api/bulk", json={"text": "  "}).status_code == 400

    def raise_lookup(_t: str) -> ResolvedBatch:
        raise LookupError("spotify not configured")

    c2 = _bulk_client(tmp_path, raise_lookup)
    assert (
        c2.post("/api/bulk", json={"text": "https://open.spotify.com/playlist/x"}).status_code
        == 503
    )


def test_batch_pause_cancel_and_retry(tmp_path: Path) -> None:
    store = RequestStore(connect(tmp_path / "b2.db"), requested_via="console")
    client = TestClient(
        create_app(store, bulk_resolve=lambda t: ResolvedBatch("PL", "paste", "", _ids("a", "b")))
    )
    bid = client.post("/api/bulk", json={"text": "a\nb"}).json()["id"]
    assert client.post(f"/api/batches/{bid}/pause", json={"paused": True}).json()["paused"] is True
    assert client.post(f"/api/batches/{bid}/cancel").json()["cancelled"] is True
    # cancelled queued request can be retried
    rid = store.get_batch(bid)["tracks"][0]["id"]
    assert client.post(f"/api/requests/{rid}/retry").status_code == 200
    # 404s / 409s
    assert client.post("/api/batches/nope/cancel").status_code == 404
    assert client.get("/api/batches/nope").status_code == 404
    assert client.post("/api/requests/nope/retry").status_code == 409


def test_validate_source_returns_validator_result(tmp_path: Path) -> None:
    seen: dict[str, object] = {}

    def validator(name: str, query: str | None) -> dict[str, object]:
        seen["name"], seen["query"] = name, query
        return {
            "ok": True,
            "detail": "downloaded “Get Lucky” — flac",
            "quality_label": "FLAC 16/44",
        }

    client = TestClient(create_app(_seeded_store(tmp_path), source_validator=validator))
    r = client.post("/api/sources/qobuz/validate")
    assert r.status_code == 200
    assert r.json()["ok"] is True and "Get Lucky" in r.json()["detail"]
    assert seen["name"] == "qobuz"
    assert seen["query"] is None  # no body -> the known test track


def test_validate_source_forces_a_supplied_query(tmp_path: Path) -> None:
    seen: dict[str, object] = {}

    def validator(name: str, query: str | None) -> dict[str, object]:
        seen["name"], seen["query"] = name, query
        return {"ok": False, "detail": "no results"}

    client = TestClient(create_app(_seeded_store(tmp_path), source_validator=validator))
    r = client.post("/api/sources/soulseek/validate", json={"query": "  Jay Sean Ride It  "})
    assert r.status_code == 200
    assert seen["name"] == "soulseek"
    assert seen["query"] == "Jay Sean Ride It"  # trimmed


def test_validate_source_blank_query_falls_back_to_test_track(tmp_path: Path) -> None:
    seen: dict[str, object] = {}

    def validator(name: str, query: str | None) -> dict[str, object]:
        seen["query"] = query
        return {"ok": True, "detail": "ok"}

    client = TestClient(create_app(_seeded_store(tmp_path), source_validator=validator))
    client.post("/api/sources/qobuz/validate", json={"query": "   "})
    assert seen["query"] is None


def test_validate_source_unknown_source_404(tmp_path: Path) -> None:
    client = TestClient(
        create_app(_seeded_store(tmp_path), source_validator=lambda n, q: {"ok": True})
    )
    assert client.post("/api/sources/nope/validate").status_code == 404


def test_validate_source_503_without_validator(tmp_path: Path) -> None:
    client = TestClient(create_app(_seeded_store(tmp_path)))
    assert client.post("/api/sources/qobuz/validate").status_code == 503


def test_me_reports_missing_scopes_for_session_user(tmp_path: Path) -> None:
    port = _FakePort(connected=True)
    port.missing = ["user-top-read"]
    client = TestClient(create_app(_seeded_store(tmp_path), auth=_FakeAuth(), spotify=port))
    me = client.get("/api/me", cookies={"crate_session": _FakeAuth.RAW}).json()
    assert me["spotify_id"] == "spot1"
    assert me["missing_scopes"] == ["user-top-read"]  # -> the console re-login banner


def test_me_missing_scopes_degrades_to_empty(tmp_path: Path) -> None:
    port = _FakePort(connected=True)
    port.boom = True  # a vault hiccup must not break /api/me
    client = TestClient(create_app(_seeded_store(tmp_path), auth=_FakeAuth(), spotify=port))
    me = client.get("/api/me", cookies={"crate_session": _FakeAuth.RAW}).json()
    assert me["missing_scopes"] == []
    # and without a spotify port configured at all
    bare = TestClient(create_app(_seeded_store(tmp_path), auth=_FakeAuth()))
    assert (
        bare.get("/api/me", cookies={"crate_session": _FakeAuth.RAW}).json()["missing_scopes"] == []
    )
