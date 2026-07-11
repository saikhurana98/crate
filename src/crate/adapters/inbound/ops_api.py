"""Management-console HTTP API (ADR-0018).

FastAPI over a read-only :class:`RequestStore` (stats/requests/live) plus an
optional :class:`ConfigStore` for the Phase-2 cascade config (tier layout,
strategies, per-source settings + encrypted credentials). The console process is
a separate reader/writer on the same WAL DB the shim reads for hot-reload; live
updates are served by polling the DB in ``/api/events`` (no cross-process bus).

Auth: an ``X-API-Key`` header, injected by the reverse proxy (Caddy) so the
browser never handles the key. ``/api/health`` is unauthenticated for probes.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import time
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any, Protocol

from fastapi import APIRouter, Body, Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from crate.application.bulk import ResolvedBatch
from crate.composition.catalog import (
    KNOWN,
    PINNED,
    TIER_OF,
    LayoutError,
    default_layout,
    source_status,
    validate_layout,
)
from crate.domain.candidate import Candidate
from crate.domain.identity import TrackIdentity
from crate.domain.matching import candidate_match
from crate.infrastructure.sqlite.config_store import ConfigStore
from crate.infrastructure.sqlite.store import RequestStore

SourcesFn = Callable[[], list[dict[str, Any]]]
SearchFn = Callable[[str, int], list[dict[str, Any]]]
BulkResolveFn = Callable[[str], ResolvedBatch]
# Review-tab candidate sources: name -> (search_candidates, download_candidate).
# Only the search half is used by the console; the serve worker does downloads.
CandidateSearchFn = Callable[[str, "TrackIdentity | None"], list[Candidate]]
CandidateSourcesFn = Callable[[], dict[str, tuple[CandidateSearchFn, Any]]]

_SEARCH_TTL_S = 20.0
_SEARCH_CACHE_MAX = 128
_MAX_PENDING_STATES = 32  # cap the CSRF-state set; single-user tool, bounded churn
_REVIEW_SOURCE_TIMEOUT_S = 12.0  # per-source ceiling on the review search fan-out


class SpotifyPort(Protocol):
    """Console-facing Spotify player integration (user-scoped OAuth)."""

    def connected(self) -> bool:
        """True once the operator has authorized (a refresh token is stored)."""
        ...

    def authorize_url(self, state: str) -> str:
        """The Spotify consent URL to redirect the browser to on Connect."""
        ...

    def connect(self, code: str) -> None:
        """Exchange the callback code for a refresh token and persist it."""
        ...

    def player(self) -> dict[str, Any]:
        """``{now_playing, recent}`` — the operator's current + recent tracks."""
        ...


def create_app(
    store: RequestStore,
    *,
    api_key: str | None = None,
    static_dir: Path | None = None,
    sources: SourcesFn = source_status,
    config: ConfigStore | None = None,
    search: SearchFn | None = None,
    spotify: SpotifyPort | None = None,
    bulk_resolve: BulkResolveFn | None = None,
    candidate_sources: CandidateSourcesFn | None = None,
    poll_interval: float = 2.0,
) -> FastAPI:
    app = FastAPI(title="crate console", docs_url=None, redoc_url=None, openapi_url=None)

    def require_key(x_api_key: str | None = Header(default=None)) -> None:
        if api_key and x_api_key != api_key:
            raise HTTPException(status_code=401, detail="unauthorized")

    def sources_view() -> list[dict[str, Any]]:
        """Source records reflecting the stored layout (membership => enabled) and
        stored credentials (=> configured), falling back to the injected default."""
        if config is None:
            return sources()
        layout = config.get_layout() or default_layout()
        in_tier = {name for t in layout["tiers"] for name in t["sources"]}
        return source_status(
            enabled_of=lambda n: n in in_tier,
            has_secret=lambda n, k: k in config.secret_keys(n),
        )

    def need_config() -> ConfigStore:
        if config is None:
            raise HTTPException(status_code=503, detail="config store not available")
        return config

    api = APIRouter(prefix="/api", dependencies=[Depends(require_key)])

    @api.get("/stats")
    def stats() -> dict[str, Any]:
        return store.stats()

    @api.get("/requests")
    def requests(
        status: str | None = None,
        q: str | None = None,
        limit: int = Query(default=50, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
    ) -> list[dict[str, Any]]:
        return store.list_requests(status=status, query=q, limit=limit, offset=offset)

    @api.get("/requests/{request_id}")
    def request_detail(request_id: str) -> dict[str, Any]:
        record = store.get_request(request_id)
        if record is None:
            raise HTTPException(status_code=404, detail="request not found")
        return record

    @api.get("/sources")
    def sources_list() -> list[dict[str, Any]]:
        return sources_view()

    # short-TTL cache so debounced/repeated searches skip the Spotify round-trip
    search_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}

    @api.get("/search")
    def search_tracks(
        q: str = Query(min_length=1),
        limit: int = Query(default=20, ge=1, le=50),
    ) -> list[dict[str, Any]]:
        if search is None:
            raise HTTPException(status_code=503, detail="search not configured")
        key = f"{limit}:{q.strip().lower()}"
        now = time.monotonic()
        hit = search_cache.get(key)
        if hit is not None and hit[0] > now:
            return hit[1]
        results = search(q, limit)
        if len(search_cache) >= _SEARCH_CACHE_MAX:
            search_cache.pop(next(iter(search_cache)))
        search_cache[key] = (now + _SEARCH_TTL_S, results)
        return results

    @api.post("/download")
    def enqueue_download(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        query = str(body.get("query", "")).strip()
        if not query:
            raise HTTPException(status_code=400, detail="query required")
        rid = store.enqueue(query, identity=_identity_from_body(body))
        return {"id": rid, "query": query, "status": "queued"}

    # --- bulk downloads (batches) ---
    @api.post("/bulk")
    def enqueue_bulk(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        text = str(body.get("text", "")).strip()
        if not text:
            raise HTTPException(status_code=400, detail="text required")
        if bulk_resolve is None:
            raise HTTPException(status_code=503, detail="bulk resolver not configured")
        try:
            batch = bulk_resolve(text)
        except LookupError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:  # resolver/network failure
            raise HTTPException(status_code=502, detail=f"could not resolve: {exc}") from exc
        bid = store.create_batch(batch.name, batch.origin, batch.owner, len(batch.tracks))
        for track in batch.tracks:
            store.enqueue(track.display(), batch_id=bid, identity=track)
        return {"id": bid, "name": batch.name, "origin": batch.origin, "total": len(batch.tracks)}

    @api.get("/batches")
    def batches() -> list[dict[str, Any]]:
        return store.list_batches()

    @api.get("/batches/{batch_id}")
    def batch_detail(batch_id: str) -> dict[str, Any]:
        record = store.get_batch(batch_id)
        if record is None:
            raise HTTPException(status_code=404, detail="batch not found")
        return record

    @api.post("/batches/{batch_id}/pause")
    def pause_batch(batch_id: str, body: dict[str, Any] = Body(default={})) -> dict[str, Any]:
        paused = bool(body.get("paused", True))
        if not store.set_batch_paused(batch_id, paused):
            raise HTTPException(status_code=404, detail="batch not found")
        return {"id": batch_id, "paused": paused}

    @api.post("/batches/{batch_id}/cancel")
    def cancel_batch(batch_id: str) -> dict[str, Any]:
        if not store.cancel_batch(batch_id):
            raise HTTPException(status_code=404, detail="batch not found")
        return {"id": batch_id, "cancelled": True}

    @api.post("/requests/{request_id}/retry")
    def retry_request(request_id: str) -> dict[str, Any]:
        if not store.requeue(request_id):
            raise HTTPException(status_code=409, detail="request is not retryable")
        return {"id": request_id, "status": "queued"}

    # --- review tab (user-assisted retry / re-download) ---
    @api.get("/review")
    def review_list() -> dict[str, Any]:
        return {"items": store.list_review(), "stats": store.review_stats()}

    @api.post("/review/{request_id}/search")
    async def review_search(
        request_id: str, body: dict[str, Any] = Body(default={})
    ) -> list[dict[str, Any]]:
        record = store.get_request(request_id)
        if record is None:
            raise HTTPException(status_code=404, detail="request not found")
        query = str(body.get("q") or "").strip() or str(record["query"])
        want = _identity_from_record(record)
        sources = candidate_sources() if candidate_sources is not None else {}

        async def run_one(fn: CandidateSearchFn) -> list[Candidate]:
            try:
                return await asyncio.wait_for(
                    asyncio.to_thread(fn, query, want), timeout=_REVIEW_SOURCE_TIMEOUT_S
                )
            except Exception:  # timeout / source failure -> skip this source
                return []

        results = await asyncio.gather(*(run_one(s[0]) for s in sources.values()))
        cands = [c for lst in results for c in lst]
        rows = [
            _candidate_row(i, c, candidate_match(c.title, c.artist, c.duration_s, want))
            for i, c in enumerate(cands)
        ]
        rows.sort(key=lambda r: r["match"], reverse=True)
        return rows

    @api.post("/review/{request_id}/download")
    def review_download(request_id: str, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        record = store.get_request(request_id)
        if record is None:
            raise HTTPException(status_code=404, detail="request not found")
        candidate = _candidate_from_body(body.get("candidate"))
        if candidate is None:
            raise HTTPException(status_code=400, detail="candidate required")
        download = record.get("download") or {}
        is_upgrade = record["status"] == "done" and int(download.get("lossless") or 0) == 0
        target_path = str(download["path"]) if is_upgrade and download.get("path") else None
        if not store.queue_review_download(
            request_id, candidate, is_upgrade=is_upgrade, target_path=target_path
        ):
            raise HTTPException(status_code=404, detail="request not found")
        return {"id": request_id, "status": "queued", "source": candidate.source}

    @api.post("/review/{request_id}/dismiss")
    def review_dismiss(request_id: str) -> dict[str, Any]:
        if not store.dismiss_review(request_id):
            raise HTTPException(status_code=404, detail="request not found or already dismissed")
        return {"id": request_id, "dismissed": True}

    @api.get("/config")
    def get_config() -> dict[str, Any]:
        cfg = need_config()
        layout = cfg.get_layout()
        if layout is None:
            layout = default_layout()
            cfg.seed_layout(layout)
        return {**layout, "version": cfg.version()}

    @api.put("/config")
    def put_config(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        cfg = need_config()
        try:
            layout = validate_layout(body)
        except LayoutError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        version = cfg.put_layout(layout)
        return {**layout, "version": version}

    @api.put("/sources/{name}")
    def put_source(name: str, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        cfg = need_config()
        if name not in KNOWN:
            raise HTTPException(status_code=404, detail="unknown source")
        creds = body.get("credentials") or {}
        for env_key, value in creds.items():
            if value:  # empty => leave unchanged (never clears a stored secret)
                cfg.put_secret(name, str(env_key), str(value))
        if isinstance(body.get("settings"), dict):
            cfg.put_settings(name, body["settings"])
        if "enabled" in body:
            enabled = bool(body["enabled"])
            if name in PINNED and not enabled:
                raise HTTPException(status_code=400, detail=f"{name} is pinned; cannot disable")
            layout = cfg.get_layout() or default_layout()
            for t in layout["tiers"]:
                t["sources"] = [s for s in t["sources"] if s != name]
            layout["disabled"] = [s for s in layout["disabled"] if s != name]
            if enabled:
                tier = TIER_OF[name]
                next(t for t in layout["tiers"] if t["tier"] == tier)["sources"].append(name)
            else:
                layout["disabled"].append(name)
            cfg.put_layout(validate_layout(layout))
        return next(s for s in sources_view() if s["name"] == name)

    # --- Spotify "Listening" panel (user-scoped OAuth) ---
    pending_states: set[str] = set()

    @api.get("/spotify/status")
    def spotify_status() -> dict[str, bool]:
        return {
            "configured": spotify is not None,
            "connected": bool(spotify and spotify.connected()),
        }

    @api.get("/spotify/login")
    def spotify_login() -> RedirectResponse:
        if spotify is None:
            raise HTTPException(status_code=503, detail="spotify not configured")
        state = secrets.token_urlsafe(16)
        if len(pending_states) < _MAX_PENDING_STATES:
            pending_states.add(state)
        return RedirectResponse(spotify.authorize_url(state), status_code=307)

    @api.get("/spotify/callback")
    def spotify_callback(
        code: str | None = None, state: str | None = None, error: str | None = None
    ) -> RedirectResponse:
        if spotify is None:
            raise HTTPException(status_code=503, detail="spotify not configured")
        pending_states.discard(state or "")
        if error or not code or state is None:
            return RedirectResponse("/?spotify=error", status_code=303)
        try:
            spotify.connect(code)
        except Exception:  # bad code / exchange failure -> surface in the UI, don't 500
            return RedirectResponse("/?spotify=error", status_code=303)
        return RedirectResponse("/?spotify=connected", status_code=303)

    @api.get("/spotify/player")
    def spotify_player() -> dict[str, Any]:
        if spotify is None:
            raise HTTPException(status_code=503, detail="spotify not configured")
        if not spotify.connected():
            raise HTTPException(status_code=409, detail="not connected")
        return spotify.player()

    @api.get("/events")
    def events() -> StreamingResponse:
        return StreamingResponse(
            _event_stream(store, poll_interval), media_type="text/event-stream"
        )

    # health sits outside the authed router so proxy/uptime probes need no key
    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    app.include_router(api)
    if static_dir is not None:
        app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="ui")
    return app


def _identity_from_body(body: dict[str, Any]) -> TrackIdentity | None:
    """Build a :class:`TrackIdentity` from optional enqueue-payload fields.

    The console's search-result enqueue POSTs ``isrc``/``duration_s``/``artist``/
    ``title`` alongside ``query``; all are optional. When none are present the
    request stays a plain free-text query (``None``)."""
    artist = _clean(body.get("artist"))
    title = _clean(body.get("title"))
    isrc = _clean(body.get("isrc"))
    duration = body.get("duration_s")
    if duration is None:
        duration = body.get("duration")
    try:
        duration_s = int(duration) if duration is not None else None
    except (TypeError, ValueError):
        duration_s = None
    if not any((artist, title, isrc, duration_s)):
        return None
    return TrackIdentity(artist=artist, title=title, isrc=isrc, duration_s=duration_s)


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _identity_from_record(record: dict[str, Any]) -> TrackIdentity:
    """The wanted track identity from a stored request row (for match scoring)."""
    duration = record.get("duration_s")
    return TrackIdentity(
        artist=_clean(record.get("artist")),
        title=_clean(record.get("title")),
        isrc=_clean(record.get("isrc")),
        duration_s=int(duration) if duration is not None else None,
    )


def _candidate_row(index: int, c: Candidate, match: int) -> dict[str, Any]:
    """Serialise a candidate + its match % for the review UI. ``id`` is a stable
    per-response handle for radio selection; the full candidate is echoed back on
    download so the API caches nothing."""
    return {
        "id": f"{c.source}-{index}",
        "source": c.source,
        "tier": c.tier,
        "ref": c.ref,
        "title": c.title,
        "artist": c.artist,
        "album": c.album,
        "quality_label": c.quality_label,
        "lossless": c.lossless,
        "duration_s": c.duration_s,
        "size_bytes": c.size_bytes,
        "match": match,
    }


def _candidate_from_body(body: Any) -> Candidate | None:
    """Rebuild a :class:`Candidate` from the download payload the client echoes
    back. Returns ``None`` when the essential fields (source/ref) are missing."""
    if not isinstance(body, dict):
        return None
    source = _clean(body.get("source"))
    ref = _clean(body.get("ref"))
    if not source or not ref:
        return None
    duration = body.get("duration_s")
    size = body.get("size_bytes")
    return Candidate(
        source=source,
        tier=int(body.get("tier") or 2),
        ref=ref,
        title=str(body.get("title") or ""),
        artist=str(body.get("artist") or ""),
        album=_clean(body.get("album")),
        quality_label=str(body.get("quality_label") or ""),
        lossless=bool(body.get("lossless")),
        duration_s=int(duration) if duration is not None else None,
        size_bytes=int(size) if size is not None else None,
    )


def _live_frame(store: RequestStore) -> str:
    payload = {
        "stats": store.stats(),
        "in_flight": store.list_requests(status="downloading", limit=100),
    }
    return f"data: {json.dumps(payload)}\n\n"


async def _event_stream(  # pragma: no cover - infinite streaming loop
    store: RequestStore, poll_interval: float
) -> AsyncIterator[str]:
    while True:
        # run the (blocking) SQLite reads off the event loop, otherwise a slow
        # read under heavy write load stalls the whole console API — SSE, polls
        # and all — and the frontend appears frozen mid-download.
        yield await asyncio.to_thread(_live_frame, store)
        await asyncio.sleep(poll_interval)
