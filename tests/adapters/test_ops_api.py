"""Console ops API (FastAPI, read-only): routes, filters, auth, 404 (ADR-0018).

Drives the app with a real RequestStore over a temp DB via FastAPI's TestClient."""

from pathlib import Path

from fastapi.testclient import TestClient

from lbmd.adapters.inbound.ops_api import create_app
from lbmd.application.download import DownloadReport
from lbmd.domain.audio import AudioInfo, QualityVerdict
from lbmd.domain.download import DownloadOutcome
from lbmd.infrastructure.crypto import SecretBox
from lbmd.infrastructure.sqlite.config_store import ConfigStore
from lbmd.infrastructure.sqlite.db import connect
from lbmd.infrastructure.sqlite.store import RequestStore


def _config(tmp_path: Path) -> ConfigStore:
    return ConfigStore(connect(tmp_path / "cfg.db"), SecretBox(bytes(range(32))))


def _report(source: str = "qobuz", tier: int = 1) -> DownloadReport:
    outcome = DownloadOutcome(Path(f"/nas/{source}.flac"), tier, source, 3.0, "q")
    info = AudioInfo("flac", "flac", 96000, 2, 24, 1_800_000, 200.0, 90_000_000)
    verdict = QualityVerdict("lossless 24-bit / 96 kHz flac", True)
    return DownloadReport("q", outcome, info, verdict, None)


def _seeded_store(tmp_path: Path) -> RequestStore:
    store = RequestStore(connect(tmp_path / "lbmd.db"), requested_via="explo")
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
                "LBMD_QOBUZ_APP_ID": "a",
                "LBMD_QOBUZ_APP_SECRET": "b",
                "LBMD_QOBUZ_TOKEN": "c",
            }
        },
    )
    assert r.status_code == 200 and r.json()["credentials"] == "ok"
    assert cfg.get_secret("qobuz", "LBMD_QOBUZ_TOKEN") == "c"


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
