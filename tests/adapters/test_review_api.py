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
    assert body["stats"] == {
        "needs_review": 2,
        "failed": 1,
        "lossy": 1,
        "needs_confirm": 0,
        "resolved": 0,
    }
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


# --- fingerprint guard endpoints (ADR-0019) ---


def _lossy_at(store: RequestStore, query: str, path: str) -> str:
    rid = store.open_request(query, TrackIdentity(title=query, duration_s=200))
    store.finish_request(rid, _report(path, "ytdlp", 3, lossless=False))
    return rid


def test_download_passes_force_to_store(tmp_path: Path) -> None:
    store = _store(tmp_path)
    rid = _lossy(store, "Gosh")
    client = TestClient(create_app(store, candidate_sources=_cand_source([])))
    cand = {"source": "qobuz", "ref": "42", "title": "Gosh", "artist": "Jamie xx"}
    client.post(f"/api/review/{rid}/download", json={"candidate": cand, "force": True})
    armed = store.get_review_candidate(rid)
    assert armed is not None and armed["force"] == 1 and armed["is_upgrade"] == 1


def test_identify_returns_hint_and_caches(tmp_path: Path) -> None:
    from crate.domain.identity import IdentityHint

    store = _store(tmp_path)
    rid = _lossy(store, "Gosh")

    def fake_identify(path: Path) -> IdentityHint:
        return IdentityHint("Avicii", "Lonely Together (acoustic)", "mbid-1")

    client = TestClient(create_app(store, identify_hint=fake_identify))
    body = client.post(f"/api/review/{rid}/identify").json()
    assert body["canonical_artist"] == "Avicii"
    assert body["canonical_title"] == "Lonely Together (acoustic)"
    # cached on the request row -> surfaces in the review list
    item = next(i for i in store.list_review() if i["id"] == rid)
    assert item["acoustid_title"] == "Lonely Together (acoustic)"


def test_identify_without_identifier_returns_empty(tmp_path: Path) -> None:
    store = _store(tmp_path)
    rid = _lossy(store, "Gosh")
    client = TestClient(create_app(store))  # no identify_hint wired -> feature off
    assert client.post(f"/api/review/{rid}/identify").json() == {}


def test_audio_streams_file_with_range(tmp_path: Path) -> None:
    store = _store(tmp_path)
    audio = tmp_path / "song.opus"
    audio.write_bytes(b"OggS" + b"\x00" * 200)
    rid = _lossy_at(store, "Gosh", str(audio))
    client = TestClient(create_app(store))

    full = client.get(f"/api/review/{rid}/audio")
    assert full.status_code == 200
    assert full.headers.get("accept-ranges") == "bytes"

    partial = client.get(f"/api/review/{rid}/audio", headers={"Range": "bytes=0-3"})
    assert partial.status_code == 206
    assert partial.content == b"OggS"


def test_audio_404_when_file_missing(tmp_path: Path) -> None:
    store = _store(tmp_path)
    rid = _lossy(store, "Gosh")  # path /nas/Gosh.opus does not exist
    client = TestClient(create_app(store))
    assert client.get(f"/api/review/{rid}/audio").status_code == 404


def test_search_rows_carry_preview_url(tmp_path: Path) -> None:
    store = _store(tmp_path)
    rid = _lossy(store, "Gosh")
    cand = Candidate(
        "deezer",
        2,
        "9",
        "Gosh",
        "Jamie xx",
        "In Colour",
        "FLAC 16/44",
        True,
        311,
        None,
        preview_url="https://cdn.deezer.example/clip.mp3",
    )
    client = TestClient(create_app(store, candidate_sources=_cand_source([cand])))
    rows = client.post(f"/api/review/{rid}/search", json={}).json()
    assert rows[0]["preview_url"] == "https://cdn.deezer.example/clip.mp3"


# --- candidate audition stream proxy (Range) ---


def _streamers(mapping: dict[str, Any]) -> Any:
    def provider() -> dict[str, Any]:
        return mapping

    return provider


def test_candidate_stream_404_for_unstreamable_source(tmp_path: Path) -> None:
    store = _store(tmp_path)
    client = TestClient(create_app(store, candidate_streamers=_streamers({})))
    r = client.get("/api/review/candidate-stream", params={"source": "monochrome", "ref": "9"})
    assert r.status_code == 404


def test_candidate_stream_502_when_resolver_fails(tmp_path: Path) -> None:
    store = _store(tmp_path)

    def boom(ref: str) -> str:
        raise ValueError("bad ref")

    client = TestClient(create_app(store, candidate_streamers=_streamers({"ytdlp": boom})))
    r = client.get("/api/review/candidate-stream", params={"source": "ytdlp", "ref": "x"})
    assert r.status_code == 502


def test_candidate_stream_proxies_upstream_with_range(tmp_path: Path) -> None:
    store = _store(tmp_path)
    # A resolver returns a URL our proxy will fetch; monkeypatch is unnecessary —
    # we point the proxy at a local httpbin-like route served by a second app.
    import httpx

    body = b"AUDIOAUDIOAUDIO"

    def upstream(request: httpx.Request) -> httpx.Response:
        rng = request.headers.get("range")
        if rng == "bytes=0-4":
            return httpx.Response(
                206,
                content=body[0:5],
                headers={
                    "content-type": "audio/flac",
                    "content-range": f"bytes 0-4/{len(body)}",
                    "accept-ranges": "bytes",
                },
            )
        return httpx.Response(200, content=body, headers={"content-type": "audio/flac"})

    # Patch httpx.AsyncClient used inside the endpoint to route to our handler.
    real = httpx.AsyncClient

    def patched(*a: Any, **k: Any) -> httpx.AsyncClient:
        k["transport"] = httpx.MockTransport(upstream)
        k.pop("timeout", None)
        return real(*a, **k)

    import crate.adapters.inbound.ops_api as ops

    ops.httpx.AsyncClient = patched  # type: ignore[misc]
    try:
        client = TestClient(
            create_app(
                store, candidate_streamers=_streamers({"qobuz": lambda ref: "https://cdn/x"})
            )
        )
        r = client.get(
            "/api/review/candidate-stream",
            params={"source": "qobuz", "ref": "42"},
            headers={"Range": "bytes=0-4"},
        )
        assert r.status_code == 206
        assert r.content == b"AUDIO"
        assert r.headers.get("content-range") == f"bytes 0-4/{len(body)}"
        assert r.headers.get("accept-ranges") == "bytes"
    finally:
        ops.httpx.AsyncClient = real  # type: ignore[misc]


def test_search_rows_mark_streamable(tmp_path: Path) -> None:
    store = _store(tmp_path)
    rid = _lossy(store, "Gosh")
    cands = [
        Candidate("qobuz", 1, "1", "Gosh", "Jamie xx", None, "FLAC", True, 311, None),
        Candidate("monochrome", 1, "2", "Gosh", "Jamie xx", None, "FLAC", True, 311, None),
    ]
    client = TestClient(
        create_app(
            store,
            candidate_sources=_cand_source(cands),
            candidate_streamers=_streamers({"qobuz": lambda ref: "https://cdn/x"}),
        )
    )
    rows = {r["source"]: r for r in client.post(f"/api/review/{rid}/search", json={}).json()}
    assert rows["qobuz"]["streamable"] is True
    assert rows["monochrome"]["streamable"] is False


def test_confirm_rearms_and_requeues(tmp_path: Path) -> None:
    store = _store(tmp_path)
    rid = _lossy(store, "Gosh")
    cand = {"source": "qobuz", "ref": "42", "title": "Gosh", "artist": "Jamie xx"}
    client = TestClient(create_app(store, candidate_sources=_cand_source([])))
    client.post(f"/api/review/{rid}/download", json={"candidate": cand})
    store.claim_next()
    store.mark_needs_confirm(rid, 0.5)

    r = client.post(f"/api/review/{rid}/confirm")
    assert r.status_code == 200 and r.json()["status"] == "queued"
    armed = store.get_review_candidate(rid)
    assert armed is not None and armed["force"] == 1


def test_confirm_404_when_no_candidate(tmp_path: Path) -> None:
    store = _store(tmp_path)
    rid = _failed(store, "Nope")
    client = TestClient(create_app(store))
    assert client.post(f"/api/review/{rid}/confirm").status_code == 404


# --- direct cross-source search (not-on-Spotify fallback) ---
def test_search_sources_fans_out_and_scores(tmp_path: Path) -> None:
    cands = [
        Candidate("deezer", 2, "1", "Str8 Outta Mumbai", "Jai Paul", None, "FLAC", True, 200, None),
        Candidate("qobuz", 1, "2", "Wrong Song", "Someone", None, "FLAC", True, 200, None),
    ]
    client = TestClient(create_app(_store(tmp_path), candidate_sources=_cand_source(cands)))
    rows = client.post("/api/search/sources", json={"q": "Jai Paul Str8 Outta Mumbai"}).json()
    assert [r["source"] for r in rows] == ["deezer", "qobuz"]  # best match first
    assert rows[0]["match"] > rows[1]["match"]
    assert rows[0]["title"] == "Str8 Outta Mumbai"


def test_search_sources_validates_and_503_without_sources(tmp_path: Path) -> None:
    client = TestClient(create_app(_store(tmp_path), candidate_sources=_cand_source([])))
    assert client.post("/api/search/sources", json={"q": "x"}).status_code == 400  # too short
    # no candidate_sources wired at all -> 503
    bare = TestClient(create_app(_store(tmp_path)))
    assert bare.post("/api/search/sources", json={"q": "something"}).status_code == 503


def test_download_source_candidate_opens_request_and_arms(tmp_path: Path) -> None:
    store = _store(tmp_path)
    client = TestClient(create_app(store))
    cand = {
        "source": "deezer",
        "tier": 2,
        "ref": "42",
        "title": "Str8 Outta Mumbai",
        "artist": "Jai Paul",
        "lossless": True,
        "duration_s": 200,
    }
    r = client.post(
        "/api/search/sources/download",
        json={"query": "Jai Paul Str8 Outta Mumbai", "candidate": cand, "artist": "Jai Paul"},
    )
    assert r.status_code == 200
    out = r.json()
    assert out["status"] == "queued" and out["source"] == "deezer"
    # a queued request exists, attributed, with the candidate armed for the worker
    queued = store.list_requests(status="queued")
    assert len(queued) == 1 and queued[0]["id"] == out["id"]
    armed = store.get_review_candidate(out["id"])
    assert armed is not None and armed["source"] == "deezer" and armed["is_upgrade"] == 0


def test_download_source_candidate_validation(tmp_path: Path) -> None:
    client = TestClient(create_app(_store(tmp_path)))
    assert client.post("/api/search/sources/download", json={"candidate": {}}).status_code == 400
    assert (
        client.post("/api/search/sources/download", json={"query": "x"}).status_code == 400
    )  # candidate missing


# --- cross-source search cache + pre-warm ---
def _counting_source(cands: list[Candidate]) -> tuple[Any, dict[str, int]]:
    calls = {"n": 0}

    def search(q: str, want: Any) -> list[Candidate]:
        calls["n"] += 1
        return cands

    def sources() -> dict[str, tuple[Any, Any]]:
        return {"fake": (search, lambda c, o, w: None)}

    return sources, calls


def _one_cand() -> list[Candidate]:
    return [Candidate("deezer", 2, "1", "Song", "Artist", None, "FLAC", True, 200, None)]


def test_source_search_is_cached_between_calls(tmp_path: Path) -> None:
    src, calls = _counting_source(_one_cand())
    client = TestClient(create_app(_store(tmp_path), candidate_sources=src))
    a = client.post("/api/search/sources", json={"q": "hello world"}).json()
    b = client.post("/api/search/sources", json={"q": "hello world"}).json()
    assert a == b and len(a) == 1
    assert calls["n"] == 1  # second call served from cache, no second fan-out


def test_prewarm_populates_cache_for_instant_search(tmp_path: Path) -> None:
    src, calls = _counting_source(_one_cand())
    # `with` runs the app lifespan on ONE persistent loop, like uvicorn, so the
    # fire-and-forget pre-warm task survives to the follow-up search (a plain
    # TestClient tears down a fresh loop per request and would cancel it).
    with TestClient(create_app(_store(tmp_path), candidate_sources=src)) as client:
        warm = client.post("/api/search/sources/prewarm", json={"q": "warm me"}).json()
        assert warm["status"] in ("warming", "warm")
        rows = client.post("/api/search/sources", json={"q": "warm me"}).json()
    assert len(rows) == 1
    assert calls["n"] == 1  # the click reused the pre-warmed fan-out


def test_prewarm_is_deduped_and_gated(tmp_path: Path) -> None:
    src, _ = _counting_source(_one_cand())
    client = TestClient(create_app(_store(tmp_path), candidate_sources=src))
    client.post("/api/search/sources", json={"q": "abc"})  # now cached
    assert client.post("/api/search/sources/prewarm", json={"q": "abc"}).json()["status"] == "warm"
    assert client.post("/api/search/sources/prewarm", json={"q": "x"}).json()["status"] == "skip"
    # no candidate sources wired at all -> unavailable (never raises)
    bare = TestClient(create_app(_store(tmp_path)))
    assert bare.post("/api/search/sources/prewarm", json={"q": "abc"}).json()["status"] == (
        "unavailable"
    )


def test_source_search_cache_expires_after_ttl(tmp_path: Path, monkeypatch: Any) -> None:
    import crate.adapters.inbound.ops_api as mod

    clock = [1000.0]
    monkeypatch.setattr(mod.time, "monotonic", lambda: clock[0])
    src, calls = _counting_source(_one_cand())
    client = TestClient(create_app(_store(tmp_path), candidate_sources=src))
    client.post("/api/search/sources", json={"q": "tick"})
    clock[0] += 10  # still within TTL -> cached
    client.post("/api/search/sources", json={"q": "tick"})
    assert calls["n"] == 1
    clock[0] += mod._SOURCE_SEARCH_TTL_S + 1  # past TTL -> swept + re-fanned
    client.post("/api/search/sources", json={"q": "tick"})
    assert calls["n"] == 2


def test_source_search_row_includes_album_art(tmp_path: Path) -> None:
    cands = [
        Candidate(
            "deezer", 2, "1", "T", "A", None, "FLAC", True, 200, None, art_url="http://img/x.jpg"
        )
    ]
    client = TestClient(create_app(_store(tmp_path), candidate_sources=_cand_source(cands)))
    rows = client.post("/api/search/sources", json={"q": "hello"}).json()
    assert rows[0]["art"] == "http://img/x.jpg"


# --- Soulseek browser (Nicotine+-style raw browse per review item) ---
_SLSK_ROW = {
    "username": "flacguy",
    "filename": "@@b\\Music\\Jamie xx - Gosh.flac",
    "basename": "Jamie xx - Gosh.flac",
    "ext": "flac",
    "size_bytes": 30_000_000,
    "bitrate_kbps": 0,
    "bit_depth": 24,
    "sample_rate": 96000,
    "length_s": 311,
    "lossless": True,
    "free_slot": True,
    "upload_speed": 3000,
    "quality_label": "FLAC 24/96",
    "candidate": {
        "source": "soulseek",
        "tier": 2,
        "ref": '{"u": "flacguy", "f": "@@b\\\\Music\\\\Jamie xx - Gosh.flac", "s": 30000000}',
        "title": "Jamie xx - Gosh",
        "artist": "Jamie xx",
        "album": None,
        "quality_label": "FLAC 24/96",
        "lossless": True,
        "duration_s": 311,
        "size_bytes": 30_000_000,
    },
}


def test_review_soulseek_search_defaults_to_item_query_and_accepts_custom_terms(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    rid = _lossy(store, "Gosh")
    seen: list[str] = []

    def browse(term: str) -> list[dict[str, Any]]:
        seen.append(term)
        return [_SLSK_ROW]

    client = TestClient(create_app(store, soulseek_browse=browse))
    rows = client.post(f"/api/review/{rid}/soulseek/search", json={}).json()
    assert seen == ["Gosh"]  # no term -> the item's own query
    assert rows[0]["username"] == "flacguy" and rows[0]["candidate"]["source"] == "soulseek"

    client.post(f"/api/review/{rid}/soulseek/search", json={"q": "jamie xx gosh flac 24"})
    assert seen[-1] == "jamie xx gosh flac 24"  # custom terms passed through verbatim


def test_review_soulseek_search_gates(tmp_path: Path) -> None:
    store = _store(tmp_path)
    rid = _lossy(store, "Gosh")
    wired = TestClient(create_app(store, soulseek_browse=lambda term: []))
    assert wired.post("/api/review/nope/soulseek/search", json={}).status_code == 404
    assert wired.post(f"/api/review/{rid}/soulseek/search", json={"q": "x"}).status_code == 400
    # slskd not configured -> the panel hides; the endpoint says so instead of 500ing
    bare = TestClient(create_app(store))
    assert bare.post(f"/api/review/{rid}/soulseek/search", json={}).status_code == 503


def test_review_soulseek_search_surfaces_backend_failure(tmp_path: Path) -> None:
    store = _store(tmp_path)
    rid = _lossy(store, "Gosh")

    def boom(term: str) -> list[dict[str, Any]]:
        raise RuntimeError("slskd down")

    client = TestClient(create_app(store, soulseek_browse=boom))
    r = client.post(f"/api/review/{rid}/soulseek/search", json={})
    assert r.status_code == 502 and "slskd down" in r.json()["detail"]


def test_soulseek_picked_file_downloads_via_the_normal_review_path(tmp_path: Path) -> None:
    """The browser's candidate round-trips through /review/{id}/download: the
    JSON ref (peer + backslash path + size) survives and the source is armed."""
    store = _store(tmp_path)
    rid = _lossy(store, "Gosh")
    client = TestClient(create_app(store))
    r = client.post(f"/api/review/{rid}/download", json={"candidate": _SLSK_ROW["candidate"]})
    assert r.status_code == 200 and r.json()["source"] == "soulseek"
    armed = store.get_review_candidate(rid)
    assert armed is not None and armed["source"] == "soulseek"
    assert armed["ref"] == _SLSK_ROW["candidate"]["ref"]  # opaque ref preserved byte-for-byte
    assert armed["is_upgrade"] == 1  # lossy item -> replace in place


def test_generic_fanout_skips_soulseek(tmp_path: Path) -> None:
    """Soulseek is a candidate source (for download-by-ref) but the generic
    review / cross-source fan-out never calls its slow P2P search."""
    called: list[str] = []
    cands = [Candidate("deezer", 2, "1", "Gosh", "Jamie xx", None, "FLAC", True, 311, None)]

    def sources() -> dict[str, tuple[Any, Any]]:
        def slsk_search(q: str, want: Any) -> list[Candidate]:
            called.append("soulseek")
            return []

        return {
            "soulseek": (slsk_search, lambda c, o, w: None),
            "deezer": (lambda q, w: cands, lambda c, o, w: None),
        }

    client = TestClient(create_app(_store(tmp_path), candidate_sources=sources))
    rows = client.post("/api/search/sources", json={"q": "jamie xx gosh"}).json()
    assert [r["source"] for r in rows] == ["deezer"]
    assert called == []  # soulseek's search was never invoked
