"""Review-tab API (FastAPI TestClient): list + stats, search fan-out, targeted
re-download enqueue (incl. the lossy-replace upgrade wiring), and dismiss.

Drives the real app + RequestStore over a temp DB; candidate sources are fakes."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from crate.adapters.inbound.ops_api import create_app
from crate.application.download import DownloadReport
from crate.domain.audio import AudioInfo, QualityVerdict
from crate.domain.candidate import Candidate
from crate.domain.download import DownloadOutcome
from crate.domain.identity import TrackIdentity
from crate.infrastructure.sqlite.db import connect
from crate.infrastructure.sqlite.store import RequestStore


def _store(tmp_path: Path) -> RequestStore:
    return RequestStore(connect(tmp_path / "crate.db"), requested_via="console")


def _report(path: str, source: str, tier: int, *, lossless: bool) -> DownloadReport:
    codec = "flac" if lossless else "opus"
    outcome = DownloadOutcome(Path(path), tier, source, 3.0, "q")
    info = AudioInfo(codec, codec, 44100, 2, 16 if lossless else None, 900_000, 200.0, 5_000_000)
    verdict = QualityVerdict("lossless" if lossless else "lossy", lossless)
    return DownloadReport("q", outcome, info, verdict, None)


def _lossy(store: RequestStore, query: str) -> str:
    rid = store.open_request(query, TrackIdentity(artist="Jamie xx", title="Gosh", duration_s=311))
    store.finish_request(rid, _report(f"/nas/{query}.opus", "ytdlp", 3, lossless=False))
    return rid


def _failed(store: RequestStore, query: str) -> str:
    rid = store.open_request(query, TrackIdentity(artist="Jai Paul", title="Str8 Outta Mumbai"))
    store.fail_request(rid, "all tiers missed")
    return rid


def _cand_source(cands: list[Candidate]) -> Any:
    def sources() -> dict[str, tuple[Any, Any]]:
        return {"fake": (lambda q, want: cands, lambda c, o, w: None)}

    return sources


def test_review_list_returns_items_and_stats(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _failed(store, "Str8 Outta Mumbai")
    _lossy(store, "Gosh")
    client = TestClient(create_app(store))

    body = client.get("/api/review").json()
    assert body["stats"] == {"needs_review": 2, "failed": 1, "lossy": 1, "resolved": 0}
    kinds = sorted(i["kind"] for i in body["items"])
    assert kinds == ["failed", "lossy"]


def test_review_search_fans_out_and_sorts_by_match(tmp_path: Path) -> None:
    store = _store(tmp_path)
    rid = _lossy(store, "Gosh")
    cands = [
        Candidate("deezer", 2, "1", "Gosh", "Jamie xx", "In Colour", "FLAC", True, 311, None),
        Candidate(
            "ytdlp", 3, "v", "Gosh (Live Edit)", "Someone Else", None, "opus", False, 260, None
        ),
    ]
    client = TestClient(create_app(store, candidate_sources=_cand_source(cands)))

    rows = client.post(f"/api/review/{rid}/search", json={}).json()
    assert [r["source"] for r in rows] == ["deezer", "ytdlp"]  # exact match sorts first
    assert rows[0]["match"] > rows[1]["match"]
    assert rows[0]["id"] == "deezer-0"


def test_review_search_skips_failing_source(tmp_path: Path) -> None:
    store = _store(tmp_path)
    rid = _lossy(store, "Gosh")

    def boom(q: str, want: object) -> list[Candidate]:
        raise RuntimeError("source down")

    ok = [Candidate("deezer", 2, "1", "Gosh", "Jamie xx", None, "FLAC", True, 311, None)]

    def sources() -> dict[str, tuple[Any, Any]]:
        return {"bad": (boom, None), "good": (lambda q, w: ok, None)}

    client = TestClient(create_app(store, candidate_sources=sources))
    rows = client.post(f"/api/review/{rid}/search", json={}).json()
    assert [r["source"] for r in rows] == ["deezer"]  # bad source skipped, not fatal


def test_review_search_404_for_unknown_request(tmp_path: Path) -> None:
    client = TestClient(create_app(_store(tmp_path)))
    assert client.post("/api/review/nope/search", json={}).status_code == 404


def test_review_download_upgrade_arms_candidate_with_target(tmp_path: Path) -> None:
    store = _store(tmp_path)
    rid = _lossy(store, "Gosh")
    client = TestClient(create_app(store))

    cand = {
        "source": "deezer",
        "tier": 2,
        "ref": "1",
        "title": "Gosh",
        "artist": "Jamie xx",
        "album": "In Colour",
        "quality_label": "FLAC 16/44",
        "lossless": True,
        "duration_s": 311,
    }
    r = client.post(f"/api/review/{rid}/download", json={"candidate": cand})
    assert r.status_code == 200 and r.json()["status"] == "queued"

    armed = store.get_review_candidate(rid)
    assert armed is not None
    assert armed["is_upgrade"] == 1  # lossy done row -> upgrade
    assert armed["target_path"] == "/nas/Gosh.opus"  # the current lossy file
    assert store.get_request(rid)["status"] == "queued"  # type: ignore[index]


def test_review_download_failed_request_is_not_upgrade(tmp_path: Path) -> None:
    store = _store(tmp_path)
    rid = _failed(store, "Str8 Outta Mumbai")
    client = TestClient(create_app(store))
    cand = {
        "source": "qobuz",
        "tier": 1,
        "ref": "9",
        "title": "Str8 Outta Mumbai",
        "artist": "Jai Paul",
    }
    r = client.post(f"/api/review/{rid}/download", json={"candidate": cand})
    assert r.status_code == 200
    armed = store.get_review_candidate(rid)
    assert armed is not None and armed["is_upgrade"] == 0 and armed["target_path"] is None


def test_review_download_requires_candidate(tmp_path: Path) -> None:
    store = _store(tmp_path)
    rid = _failed(store, "X")
    client = TestClient(create_app(store))
    assert client.post(f"/api/review/{rid}/download", json={}).status_code == 400
    assert (
        client.post(
            "/api/review/nope/download", json={"candidate": {"source": "q", "ref": "1"}}
        ).status_code
        == 404
    )


def test_review_dismiss(tmp_path: Path) -> None:
    store = _store(tmp_path)
    rid = _failed(store, "Dismiss Me")
    client = TestClient(create_app(store))
    assert client.post(f"/api/review/{rid}/dismiss").json()["dismissed"] is True
    assert client.get("/api/review").json()["items"] == []
    assert client.post(f"/api/review/{rid}/dismiss").status_code == 404  # already gone


def test_review_search_default_query_uses_request(tmp_path: Path) -> None:
    store = _store(tmp_path)
    rid = _lossy(store, "Gosh")
    seen: dict[str, str] = {}

    def search(q: str, want: object) -> list[Candidate]:
        seen["q"] = q
        return []

    def sources() -> dict[str, tuple[Any, Any]]:
        return {"s": (search, None)}

    client = TestClient(create_app(store, candidate_sources=sources))
    client.post(f"/api/review/{rid}/search", json={})  # no q -> falls back to request query
    assert seen["q"] == "Gosh"
    client.post(f"/api/review/{rid}/search", json={"q": "custom terms"})
    assert seen["q"] == "custom terms"
