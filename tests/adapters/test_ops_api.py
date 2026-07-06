"""Console ops API (FastAPI, read-only): routes, filters, auth, 404 (ADR-0018).

Drives the app with a real RequestStore over a temp DB via FastAPI's TestClient."""

from pathlib import Path

from fastapi.testclient import TestClient

from crate.adapters.inbound.ops_api import create_app
from crate.application.download import DownloadReport
from crate.domain.audio import AudioInfo, QualityVerdict
from crate.domain.download import DownloadOutcome
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
    assert store.claim_next() == {"id": rid, "query": "Daft Punk One More Time"}


def test_download_requires_nonempty_query(tmp_path: Path) -> None:
    client = TestClient(create_app(_seeded_store(tmp_path)))
    assert client.post("/api/download", json={"query": "   "}).status_code == 400
    assert client.post("/api/download", json={}).status_code == 400


def test_api_key_enforced(tmp_path: Path) -> None:
    client = TestClient(create_app(_seeded_store(tmp_path), api_key="secret"))
    assert client.get("/api/stats").status_code == 401
    assert client.get("/api/stats", headers={"X-API-Key": "wrong"}).status_code == 401
    assert client.get("/api/stats", headers={"X-API-Key": "secret"}).status_code == 200


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


# --- Spotify "Listening" panel routes (user-scoped OAuth) ---


class _FakePort:
    def __init__(self, connected: bool = False, raise_on_connect: bool = False) -> None:
        self._connected = connected
        self._raise = raise_on_connect
        self.codes: list[str] = []

    def connected(self) -> bool:
        return self._connected

    def authorize_url(self, state: str) -> str:
        return f"https://accounts.spotify.com/authorize?state={state}"

    def connect(self, code: str) -> None:
        if self._raise:
            raise RuntimeError("bad code")
        self.codes.append(code)
        self._connected = True

    def player(self) -> dict[str, object]:
        return {"now_playing": {"title": "X", "query": "a X"}, "recent": []}


def test_spotify_status_absent_vs_present(tmp_path: Path) -> None:
    off = TestClient(create_app(_seeded_store(tmp_path)))
    assert off.get("/api/spotify/status").json() == {"configured": False, "connected": False}
    on = TestClient(create_app(_seeded_store(tmp_path), spotify=_FakePort(connected=True)))
    assert on.get("/api/spotify/status").json() == {"configured": True, "connected": True}


def test_spotify_login_redirects_to_consent(tmp_path: Path) -> None:
    client = TestClient(create_app(_seeded_store(tmp_path), spotify=_FakePort()))
    r = client.get("/api/spotify/login", follow_redirects=False)
    assert r.status_code == 307
    assert r.headers["location"].startswith("https://accounts.spotify.com/authorize?state=")


def test_spotify_login_503_when_absent(tmp_path: Path) -> None:
    client = TestClient(create_app(_seeded_store(tmp_path)))
    assert client.get("/api/spotify/login", follow_redirects=False).status_code == 503


def test_spotify_callback_connects(tmp_path: Path) -> None:
    port = _FakePort()
    client = TestClient(create_app(_seeded_store(tmp_path), spotify=port))
    r = client.get(
        "/api/spotify/callback", params={"code": "abc", "state": "s"}, follow_redirects=False
    )
    assert r.status_code == 303 and r.headers["location"] == "/?spotify=connected"
    assert port.codes == ["abc"]


def test_spotify_callback_error_paths(tmp_path: Path) -> None:
    port = _FakePort()
    client = TestClient(create_app(_seeded_store(tmp_path), spotify=port))
    # explicit error from Spotify
    r1 = client.get(
        "/api/spotify/callback", params={"error": "access_denied"}, follow_redirects=False
    )
    assert r1.status_code == 303 and r1.headers["location"] == "/?spotify=error"
    # exchange failure -> error, not a 500
    bad = _FakePort(raise_on_connect=True)
    c2 = TestClient(create_app(_seeded_store(tmp_path), spotify=bad))
    r2 = c2.get("/api/spotify/callback", params={"code": "x", "state": "s"}, follow_redirects=False)
    assert r2.status_code == 303 and r2.headers["location"] == "/?spotify=error"
    assert port.codes == []


def test_spotify_player_states(tmp_path: Path) -> None:
    # absent -> 503
    off = TestClient(create_app(_seeded_store(tmp_path)))
    assert off.get("/api/spotify/player").status_code == 503
    # configured but not connected -> 409
    disc = TestClient(create_app(_seeded_store(tmp_path), spotify=_FakePort(connected=False)))
    assert disc.get("/api/spotify/player").status_code == 409
    # connected -> payload
    on = TestClient(create_app(_seeded_store(tmp_path), spotify=_FakePort(connected=True)))
    body = on.get("/api/spotify/player").json()
    assert body["now_playing"]["title"] == "X" and body["recent"] == []


# --- bulk downloads ---
from crate.application.bulk import ResolvedBatch  # noqa: E402


def _bulk_client(tmp_path: Path, resolver=None) -> TestClient:
    store = RequestStore(connect(tmp_path / "bulk.db"), requested_via="console")
    return TestClient(create_app(store, bulk_resolve=resolver))


def test_bulk_creates_batch_and_enqueues(tmp_path: Path) -> None:
    resolver = lambda text: ResolvedBatch("My PL", "paste", "", ["a x", "b y", "c z"])  # noqa: E731
    client = _bulk_client(tmp_path, resolver)
    r = client.post("/api/bulk", json={"text": "a x\nb y\nc z"})
    assert r.status_code == 200
    body = r.json()
    assert body["origin"] == "paste" and body["total"] == 3
    batches = client.get("/api/batches").json()
    assert len(batches) == 1 and batches[0]["counts"]["queued"] == 3
    detail = client.get(f"/api/batches/{body['id']}").json()
    assert len(detail["tracks"]) == 3


def test_bulk_error_mapping(tmp_path: Path) -> None:
    # no resolver -> 503
    assert _bulk_client(tmp_path).post("/api/bulk", json={"text": "x"}).status_code == 503
    # empty text -> 400
    c = _bulk_client(tmp_path, lambda t: ResolvedBatch("n", "paste", "", ["a"]))
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
        create_app(store, bulk_resolve=lambda t: ResolvedBatch("PL", "paste", "", ["a", "b"]))
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
