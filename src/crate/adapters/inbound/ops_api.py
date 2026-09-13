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
import contextlib
import json
import secrets
import time
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any, Protocol

import httpx
from fastapi import APIRouter, Body, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.requests import HTTPConnection

from crate.adapters.inbound.ops_jobs import JobsPort, jobs_router, mount_jobs_ws
from crate.adapters.inbound.ops_sync import (
    DEFAULT_UPLOAD_MAX_BYTES,
    ListeningPort,
    SyncPort,
    history_router,
    sync_router,
)
from crate.application.auth import SERVICE_USER_ID, SESSION_TTL_S, User
from crate.application.bulk import ResolvedBatch
from crate.application.library_import import to_identity
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
from crate.domain.identity import IdentityHint, TrackIdentity, from_query
from crate.domain.jobs import JobKind
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
# Advisory review hint: existing file path -> IdentityHint (or None). None when the
# feature is off (no fpcalc binary / no CRATE_ACOUSTID_KEY) — the endpoint returns {}.
IdentifyFn = Callable[[Path], "IdentityHint | None"]
# Candidate-stream resolvers: source name -> (ref -> direct streamable media URL).
# The proxy only ever fetches a URL a resolver produced from a source+ref, never a
# client-supplied URL — the SSRF boundary for the audition endpoint.
StreamResolver = Callable[[str], str]
CandidateStreamersFn = Callable[[], dict[str, StreamResolver]]
# Credential test: source name -> {ok, detail, quality_label, elapsed_s}.
SourceValidatorFn = Callable[[str, "str | None"], dict[str, Any]]
# Raw Soulseek browser (Nicotine+-style): search term -> every file the network
# returned, each row carrying a ready-to-echo download candidate. None = feature off.
SoulseekBrowseFn = Callable[[str], list[dict[str, Any]]]

_SEARCH_TTL_S = 20.0
_SEARCH_CACHE_MAX = 128
_MAX_PENDING_STATES = 32  # cap the CSRF-state set; single-user tool, bounded churn
_REVIEW_SOURCE_TIMEOUT_S = 12.0  # per-source ceiling on the review search fan-out
_MIN_SOURCE_QUERY_LEN = 2  # shortest cross-source search query worth fanning out
# Cross-source search is expensive (a live fan-out with per-source timeouts), so
# results are cached and can be pre-warmed while the user is still typing the
# Spotify search. Entries expire after the TTL; the cache is bounded and swept
# (lazily on every access + periodically) so nothing orphans.
_SOURCE_SEARCH_TTL_S = 120.0
_SOURCE_SEARCH_MAX = 64  # cap distinct cached queries (evict soonest-to-expire)
_SOURCE_INFLIGHT_MAX = 8  # cap concurrent background pre-warm fan-outs
_SOURCE_SWEEP_INTERVAL_S = 60.0  # background orphan-sweep cadence
# Sources the generic review/cross-source fan-out skips: Soulseek search is slow
# and noisy P2P, so it gets its own dedicated browser instead of gating every scan.
_FANOUT_EXCLUDE = frozenset({"soulseek"})
_SLSK_BROWSE_TIMEOUT_S = 45.0  # slskd searches poll up to ~30 s; bound the endpoint
_SESSION_COOKIE = "crate_session"


class AuthPort(Protocol):
    """Login-with-Spotify provider (Phase 2). ``None`` in single-user mode."""

    def login_url(self, state: str) -> str:
        """Spotify consent URL to redirect the browser to on login."""
        ...

    def complete_login(self, code: str) -> tuple[User, str]:
        """Exchange the callback code; return the user + raw session token."""
        ...

    def resolve_session(self, raw_token: str) -> User | None:
        """The user for a live session cookie, else ``None``."""
        ...

    def logout(self, raw_token: str) -> None:
        """Revoke a session (idempotent)."""
        ...


class SpotifyPort(Protocol):
    """Per-user Spotify "Listening" panel. Tokens come from Login-with-Spotify;
    there is no separate connect flow (Phase 2 consolidation)."""

    def connected(self, user_id: str) -> bool:
        """True when this user has a stored Spotify token (always so once they've
        logged in with Spotify)."""
        ...

    def player(self, user_id: str) -> dict[str, Any]:
        """``{now_playing, recent}`` — this user's current + recent tracks."""
        ...

    def missing_scopes(self, user_id: str) -> list[str]:
        """OAuth scopes this user's token lacks for current features (→ re-login)."""
        ...


class LibraryPort(Protocol):
    """Per-user Spotify library reads for import (Phase 2)."""

    def my_playlists(self, user_id: str) -> list[dict[str, Any]]:
        """The user's playlists as ``{id, name, count, image, snapshot_id, owner_id}`` dicts."""
        ...

    def liked_songs(self, user_id: str, max_tracks: int = 2000) -> list[dict[str, Any]]:
        """The user's Liked Songs as track dicts."""
        ...

    def playlist_tracks(
        self, user_id: str, playlist_id: str, max_tracks: int = 2000
    ) -> list[dict[str, Any]]:
        """The tracks of one of the user's playlists as track dicts."""
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
    library: LibraryPort | None = None,
    auth: AuthPort | None = None,
    bulk_resolve: BulkResolveFn | None = None,
    candidate_sources: CandidateSourcesFn | None = None,
    identify_hint: IdentifyFn | None = None,
    candidate_streamers: CandidateStreamersFn | None = None,
    source_validator: SourceValidatorFn | None = None,
    soulseek_browse: SoulseekBrowseFn | None = None,
    jobs: JobsPort | None = None,
    manual_job_kinds: frozenset[JobKind] = frozenset(),
    ws_poll_interval: float = 1.0,
    ws_ping_interval: float = 25.0,
    listening: ListeningPort | None = None,
    uploads_dir: Path | None = None,
    history_upload_max_bytes: int = DEFAULT_UPLOAD_MAX_BYTES,
    sync: SyncPort | None = None,
    sync_interval_s: float = 7200.0,
    sync_preview: Callable[[str], str | None] | None = None,
    poll_interval: float = 2.0,
) -> FastAPI:
    # --- cross-source search cache (pre-warm while typing; TTL + orphan sweep) ---
    # query -> (expiry_monotonic, rows); an in-flight fan-out is tracked separately
    # so a pre-warm and the eventual click share one fan-out instead of racing.
    source_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
    source_inflight: dict[str, asyncio.Task[list[dict[str, Any]]]] = {}

    def _norm_q(q: str) -> str:
        return " ".join(q.strip().lower().split())

    def _sweep_source_cache() -> None:
        """Drop expired cache rows and forget finished in-flight tasks. Called on
        every cache access and periodically by the lifespan sweeper, so neither the
        cache nor the task map ever accumulates orphans."""
        now = time.monotonic()
        for key in [k for k, (exp, _) in source_cache.items() if exp <= now]:
            source_cache.pop(key, None)
        for key in [k for k, task in source_inflight.items() if task.done()]:
            source_inflight.pop(key, None)

    @contextlib.asynccontextmanager
    async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
        async def sweeper() -> None:  # pragma: no cover - background loop
            try:
                while True:
                    await asyncio.sleep(_SOURCE_SWEEP_INTERVAL_S)
                    _sweep_source_cache()
            except asyncio.CancelledError:
                pass

        task = asyncio.ensure_future(sweeper())
        try:
            yield
        finally:  # pragma: no cover - shutdown path
            task.cancel()
            for inflight in list(source_inflight.values()):  # cancel orphaned fan-outs
                inflight.cancel()

    app = FastAPI(
        title="crate console",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=_lifespan,
    )

    def _authenticate(conn: HTTPConnection) -> str | None:
        """The user id for an HTTP request **or** WebSocket upgrade, else ``None``.

        The matching ``X-API-Key`` (automation) -> the service user; otherwise, with
        an ``auth`` provider wired, a live session cookie -> that user. With no key
        AND no auth the console is open (single-user mode, as before)."""
        if api_key is not None and conn.headers.get("x-api-key") == api_key:
            return SERVICE_USER_ID
        if auth is not None:
            raw = conn.cookies.get(_SESSION_COOKIE) or ""
            user = auth.resolve_session(raw) if raw else None
            return user.id if user is not None else None
        return SERVICE_USER_ID if api_key is None else None

    def require_auth(request: Request) -> None:
        """Gate the ``/api`` router, stashing the user id on ``request.state`` for
        attribution (see :func:`_authenticate`)."""
        uid = _authenticate(request)
        if uid is None:
            raise HTTPException(status_code=401, detail="unauthorized")
        request.state.user_id = uid

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

    api = APIRouter(prefix="/api", dependencies=[Depends(require_auth)])

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
    def enqueue_download(request: Request, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        query = str(body.get("query", "")).strip()
        if not query:
            raise HTTPException(status_code=400, detail="query required")
        uid = getattr(request.state, "user_id", SERVICE_USER_ID)
        rid = store.enqueue(query, identity=_identity_from_body(body), user_id=uid)
        return {"id": rid, "query": query, "status": "queued"}

    # --- bulk downloads (batches) ---
    @api.post("/bulk")
    def enqueue_bulk(request: Request, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
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
        uid = getattr(request.state, "user_id", SERVICE_USER_ID)
        bid = store.create_batch(
            batch.name, batch.origin, batch.owner, len(batch.tracks), user_id=uid
        )
        for track in batch.tracks:
            store.enqueue(track.display(), batch_id=bid, identity=track, user_id=uid)
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

    async def _fanout_search(query: str, want: TrackIdentity) -> list[dict[str, Any]]:
        """Fan a query out across every enabled candidate source (in parallel, each
        bounded by ``_REVIEW_SOURCE_TIMEOUT_S``), score each hit against ``want``,
        and return ``_candidate_row`` dicts sorted best-match first. Shared by the
        Review re-download search and the direct cross-source search."""
        sources = {
            name: src
            for name, src in (candidate_sources() if candidate_sources is not None else {}).items()
            if name not in _FANOUT_EXCLUDE
        }

        async def run_one(fn: CandidateSearchFn) -> list[Candidate]:
            try:
                return await asyncio.wait_for(
                    asyncio.to_thread(fn, query, want), timeout=_REVIEW_SOURCE_TIMEOUT_S
                )
            except Exception:  # timeout / source failure -> skip this source
                return []

        results = await asyncio.gather(*(run_one(s[0]) for s in sources.values()))
        cands = [c for lst in results for c in lst]
        streamable = set(candidate_streamers().keys()) if candidate_streamers is not None else set()
        rows = [
            _candidate_row(
                i,
                c,
                candidate_match(c.title, c.artist, c.duration_s, want),
                streamable=bool(c.preview_url) or c.source in streamable,
            )
            for i, c in enumerate(cands)
        ]
        rows.sort(key=lambda r: r["match"], reverse=True)
        return rows

    async def _run_and_cache(key: str, query: str) -> list[dict[str, Any]]:
        """Fan out, store under ``key`` with a fresh TTL, then release the in-flight
        slot. Cache is written before the slot is released so a racing caller either
        awaits this task or sees the cached rows — never a duplicate fan-out."""
        try:
            rows = await _fanout_search(query, from_query(query))
            if len(source_cache) >= _SOURCE_SEARCH_MAX:
                source_cache.pop(min(source_cache, key=lambda k: source_cache[k][0]), None)
            source_cache[key] = (time.monotonic() + _SOURCE_SEARCH_TTL_S, rows)
            return rows
        finally:
            source_inflight.pop(key, None)

    def _spawn_source_task(key: str, query: str) -> asyncio.Task[list[dict[str, Any]]]:
        task = asyncio.ensure_future(_run_and_cache(key, query))
        source_inflight[key] = task
        # retrieve the exception even if nobody awaits (pre-warm only) so a failed
        # fan-out never surfaces as an "exception never retrieved" warning.
        task.add_done_callback(lambda t: t.cancelled() or t.exception())
        return task

    async def _source_search(query: str) -> list[dict[str, Any]]:
        """Cached cross-source fan-out: fresh cache hit → instant; a pre-warm still
        running → await it; otherwise fan out now and cache."""
        key = _norm_q(query)
        _sweep_source_cache()
        hit = source_cache.get(key)
        if hit is not None and hit[0] > time.monotonic():
            return hit[1]
        return await (source_inflight.get(key) or _spawn_source_task(key, query))

    @api.post("/review/{request_id}/search")
    async def review_search(
        request_id: str, body: dict[str, Any] = Body(default={})
    ) -> list[dict[str, Any]]:
        record = store.get_request(request_id)
        if record is None:
            raise HTTPException(status_code=404, detail="request not found")
        query = str(body.get("q") or "").strip() or str(record["query"])
        return await _fanout_search(query, _identity_from_record(record))

    @api.post("/review/{request_id}/soulseek/search")
    async def review_soulseek_search(
        request_id: str, body: dict[str, Any] = Body(default={})
    ) -> list[dict[str, Any]]:
        """The Nicotine+-style Soulseek browser for a review item: the user types
        their own term (defaults to the item's query) and gets EVERY file the
        network returned — no title gate, no auto-selection — to browse and pick
        from. Each row carries a ``candidate`` the console echoes to
        ``/review/{id}/download``, so the worker fetches exactly that file."""
        if store.get_request(request_id) is None:
            raise HTTPException(status_code=404, detail="request not found")
        if soulseek_browse is None:
            raise HTTPException(status_code=503, detail="soulseek not configured")
        record = store.get_request(request_id) or {}
        term = str(body.get("q") or "").strip() or str(record.get("query") or "")
        if len(term) < _MIN_SOURCE_QUERY_LEN:
            raise HTTPException(status_code=400, detail="search term too short")
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(soulseek_browse, term), timeout=_SLSK_BROWSE_TIMEOUT_S
            )
        except TimeoutError as exc:
            raise HTTPException(status_code=504, detail="soulseek search timed out") from exc
        except Exception as exc:  # slskd down / network
            raise HTTPException(status_code=502, detail=f"soulseek search failed: {exc}") from exc

    @api.post("/search/sources")
    async def search_sources(body: dict[str, Any] = Body(...)) -> list[dict[str, Any]]:
        """Search the linked download sources directly (Qobuz, Deezer, arcod, …) for
        a track that isn't on Spotify. Spotify stays the primary search; this is the
        "not on Spotify?" fallback. Returns scored candidates the caller can audition
        (the candidate-stream proxy) and download via ``/api/search/sources/download``."""
        if candidate_sources is None:
            raise HTTPException(status_code=503, detail="no candidate sources configured")
        query = str(body.get("q", "")).strip()
        if len(query) < _MIN_SOURCE_QUERY_LEN:
            raise HTTPException(status_code=400, detail="query too short")
        return await _source_search(query)

    @api.post("/search/sources/prewarm")
    async def prewarm_sources(body: dict[str, Any] = Body(...)) -> dict[str, str]:
        """Kick the cross-source fan-out off in the background so it's cached by the
        time the user clicks "search the sources". Returns immediately; the console
        fires this while the user is still typing the Spotify search. Idempotent per
        query (deduped against a fresh cache entry / an in-flight fan-out) and capped
        so speculative warming can't spawn unbounded work."""
        if candidate_sources is None:
            return {"status": "unavailable"}
        query = str(body.get("q", "")).strip()
        if len(query) < _MIN_SOURCE_QUERY_LEN:
            return {"status": "skip"}
        key = _norm_q(query)
        _sweep_source_cache()
        hit = source_cache.get(key)
        if hit is not None and hit[0] > time.monotonic():
            return {"status": "warm"}
        if key in source_inflight:
            return {"status": "pending"}
        if len(source_inflight) >= _SOURCE_INFLIGHT_MAX:
            return {"status": "busy"}
        _spawn_source_task(key, query)
        return {"status": "warming"}

    @api.post("/search/sources/download")
    def download_source_candidate(
        request: Request, body: dict[str, Any] = Body(...)
    ) -> dict[str, Any]:
        """Queue a download of one chosen source candidate (from ``/search/sources``)
        for a track with no Spotify entry. Opens a fresh request attributed to the
        user and arms the exact candidate, so the worker downloads precisely it
        rather than running the free-text cascade (which might mis-match)."""
        query = str(body.get("query", "")).strip()
        if not query:
            raise HTTPException(status_code=400, detail="query required")
        candidate = _candidate_from_body(body.get("candidate"))
        if candidate is None:
            raise HTTPException(status_code=400, detail="candidate required")
        uid = getattr(request.state, "user_id", SERVICE_USER_ID)
        rid = store.enqueue(query, identity=_identity_from_body(body), user_id=uid)
        store.queue_review_download(rid, candidate, is_upgrade=False, target_path=None)
        return {"id": rid, "status": "queued", "source": candidate.source}

    @api.post("/review/{request_id}/download")
    def review_download(request_id: str, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        record = store.get_request(request_id)
        if record is None:
            raise HTTPException(status_code=404, detail="request not found")
        candidate = _candidate_from_body(body.get("candidate"))
        if candidate is None:
            raise HTTPException(status_code=400, detail="candidate required")
        download = record.get("download") or {}
        # An upgrade if the current file is lossy OR the item was parked by the
        # gate (needs_confirm) — in both cases there's an existing file to replace.
        has_file = bool(download.get("path"))
        is_upgrade = has_file and record["status"] in ("done", "needs_confirm")
        target_path = str(download["path"]) if is_upgrade else None
        force = bool(body.get("force"))
        if not store.queue_review_download(
            request_id, candidate, is_upgrade=is_upgrade, target_path=target_path, force=force
        ):
            raise HTTPException(status_code=404, detail="request not found")
        return {"id": request_id, "status": "queued", "source": candidate.source}

    @api.post("/review/{request_id}/identify")
    async def review_identify(request_id: str) -> dict[str, Any]:
        """Advisory: fingerprint the item's current file and say what it looks like.

        Returns ``{}`` when there's no file, the feature is off, or AcoustID has no
        answer — it never fails the request (ADR-0019)."""
        record = store.get_request(request_id)
        if record is None:
            raise HTTPException(status_code=404, detail="request not found")
        path = (record.get("download") or {}).get("path")
        if not path or identify_hint is None:
            return {}
        try:
            hint = await asyncio.to_thread(identify_hint, Path(str(path)))
        except Exception:  # fingerprint/network hiccup — advisory only, degrade quietly
            return {}
        if hint is None:
            return {}
        store.set_identity_hint(
            request_id,
            artist=hint.canonical_artist,
            title=hint.canonical_title,
            mbid=hint.mbid,
        )
        return {
            "canonical_artist": hint.canonical_artist,
            "canonical_title": hint.canonical_title,
            "mbid": hint.mbid,
        }

    @api.get("/review/{request_id}/audio")
    def review_audio(request_id: str) -> FileResponse:
        """Stream the item's current library file (HTTP Range) for the A/B ear test.

        Path comes from the review row's download, never a query param — no
        directory traversal. FileResponse serves 206 partials so ``<audio>`` seeks."""
        record = store.get_request(request_id)
        if record is None:
            raise HTTPException(status_code=404, detail="request not found")
        path = (record.get("download") or {}).get("path")
        if not path or not Path(str(path)).is_file():
            raise HTTPException(status_code=404, detail="no audio for this request")
        return FileResponse(str(path))

    @api.get("/review/candidate-stream")
    async def review_candidate_stream(
        request: Request, source: str = Query(...), ref: str = Query(...)
    ) -> StreamingResponse:
        """Proxy a candidate's audio for the review A/B test (HTTP Range passthrough).

        The URL is resolved from ``source``+``ref`` by a trusted per-source resolver
        (signed Qobuz FLAC, yt-dlp -g) — a client can never supply the URL, so this
        can't be turned into an SSRF fetch. Range is forwarded upstream and the 206
        relayed, so only the listened bytes transfer (no full download)."""
        streamers = candidate_streamers() if candidate_streamers is not None else {}
        resolve = streamers.get(source)
        if resolve is None:
            raise HTTPException(status_code=404, detail="source not streamable")
        try:
            url = await asyncio.to_thread(resolve, ref)
        except Exception as exc:  # bad ref / resolver failure
            raise HTTPException(status_code=502, detail="could not resolve stream") from exc

        rng = request.headers.get("range")
        client = httpx.AsyncClient(timeout=httpx.Timeout(20.0, read=None), follow_redirects=True)
        upstream = await client.send(
            client.build_request("GET", url, headers={"Range": rng} if rng else {}),
            stream=True,
        )
        relayed = {
            h: upstream.headers[h]
            for h in ("content-type", "content-range", "content-length", "accept-ranges")
            if h in upstream.headers
        }
        relayed.setdefault("accept-ranges", "bytes")

        async def body() -> AsyncIterator[bytes]:
            try:
                async for chunk in upstream.aiter_bytes(64 * 1024):
                    yield chunk
            finally:
                await upstream.aclose()
                await client.aclose()

        return StreamingResponse(
            body(),
            status_code=upstream.status_code,
            headers=relayed,
            media_type=relayed.get("content-type"),
        )

    @api.post("/review/{request_id}/confirm")
    def review_confirm(request_id: str) -> dict[str, Any]:
        """Replace anyway: re-arm the stored candidate with force + requeue.

        Uses the candidate already in ``review_candidates`` (the one that failed the
        gate), so a ``needs_confirm`` item can be confirmed even after a reload."""
        if not store.force_review_download(request_id):
            raise HTTPException(status_code=404, detail="no candidate to confirm")
        return {"id": request_id, "status": "queued"}

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

    @api.post("/sources/{name}/validate")
    async def validate_source_creds(
        name: str, body: dict[str, Any] = Body(default={})
    ) -> dict[str, Any]:
        """Force a download through this source to test it.

        With no body it downloads a known song — the credential test. Pass
        ``{"query": "..."}`` to force *that* track through *this* source instead,
        which is how an operator pins down which source is broken for a track the
        cascade is failing on.

        Offloaded off the event loop — it does real network IO (and, for a browser
        source, a headed-Chromium resolve). Never raises for a bad credential; a
        failure comes back as ``{ok: false, detail: ...}``."""
        if name not in KNOWN:
            raise HTTPException(status_code=404, detail="unknown source")
        if source_validator is None:
            raise HTTPException(status_code=503, detail="validation not available")
        query = str(body.get("query", "")).strip() or None
        return await asyncio.to_thread(source_validator, name, query)

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

    # CSRF-state set for Login-with-Spotify (auth/login -> auth/callback).
    pending_states: set[str] = set()

    # --- Spotify "Listening" panel (per-user; token comes from login) ---
    @api.get("/spotify/status")
    def spotify_status(request: Request) -> dict[str, bool]:
        uid = getattr(request.state, "user_id", SERVICE_USER_ID)
        return {
            "configured": spotify is not None,
            "connected": bool(spotify and spotify.connected(uid)),
        }

    @api.get("/spotify/player")
    def spotify_player(request: Request) -> dict[str, Any]:
        uid = getattr(request.state, "user_id", SERVICE_USER_ID)
        if spotify is None or not spotify.connected(uid):
            raise HTTPException(status_code=409, detail="not connected")
        return spotify.player(uid)

    # --- Spotify library import (per-user) ---
    @api.get("/library/playlists")
    def library_playlists(request: Request) -> list[dict[str, Any]]:
        if library is None:
            raise HTTPException(status_code=503, detail="library not configured")
        uid = getattr(request.state, "user_id", SERVICE_USER_ID)
        return library.my_playlists(uid)

    @api.post("/library/import")
    def library_import(request: Request, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        if library is None:
            raise HTTPException(status_code=503, detail="library not configured")
        uid = getattr(request.state, "user_id", SERVICE_USER_ID)
        source = str(body.get("source", "")).strip()
        try:
            if source == "liked":
                name, tracks = "Liked Songs", library.liked_songs(uid)
            elif source == "playlist":
                pid = str(body.get("playlist_id", "")).strip()
                if not pid:
                    raise HTTPException(status_code=400, detail="playlist_id required")
                name = str(body.get("name", "")).strip() or "Playlist"
                tracks = library.playlist_tracks(uid, pid)
            else:
                raise HTTPException(status_code=400, detail="source must be 'liked' or 'playlist'")
        except HTTPException:
            raise
        except Exception as exc:  # token/refresh/network failure
            raise HTTPException(status_code=502, detail=f"import failed: {exc}") from exc
        if not tracks:
            raise HTTPException(status_code=404, detail="nothing to import")
        bid = store.create_batch(name, "spotify", name, len(tracks), user_id=uid)
        for track in tracks:
            store.enqueue(track["query"], batch_id=bid, identity=to_identity(track), user_id=uid)
        return {"id": bid, "name": name, "origin": "spotify", "total": len(tracks)}

    @api.get("/feed")
    def feed(
        limit: int = Query(default=50, ge=1, le=200),
        offset: int = Query(default=0, ge=0),
    ) -> list[dict[str, Any]]:
        """The shared activity feed — everyone sees everything: who requested each
        track and how fast it was served (time-to-first-playable + time-to-FLAC)."""
        return store.list_feed(limit=limit, offset=offset)

    @api.get("/events")
    def events() -> StreamingResponse:
        return StreamingResponse(
            _event_stream(store, poll_interval), media_type="text/event-stream"
        )

    @api.get("/me")
    def me(request: Request) -> dict[str, Any]:
        """The logged-in user (behind require_auth). A live session resolves the
        full profile; the API-key / single-user path returns the service user."""
        raw = request.cookies.get(_SESSION_COOKIE) or ""
        user = auth.resolve_session(raw) if auth is not None and raw else None
        if user is not None:
            out = _user_dict(user)
            try:
                out["missing_scopes"] = spotify.missing_scopes(user.id) if spotify else []
            except Exception:  # vault/provider hiccup must not break /api/me
                out["missing_scopes"] = []
            return out
        uid = getattr(request.state, "user_id", SERVICE_USER_ID)
        return {
            "id": uid,
            "spotify_id": None,
            "display_name": "service" if uid == SERVICE_USER_ID else uid,
            "email": None,
            "avatar_url": None,
            "is_admin": False,
            "missing_scopes": [],
        }

    # --- Login with Spotify (unauthenticated: these establish the session) ---
    @app.get("/api/auth/login")
    def auth_login() -> RedirectResponse:
        if auth is None:
            raise HTTPException(status_code=503, detail="login not configured")
        state = secrets.token_urlsafe(16)
        if len(pending_states) < _MAX_PENDING_STATES:
            pending_states.add(state)
        return RedirectResponse(auth.login_url(state), status_code=307)

    @app.get("/api/auth/callback")
    def auth_callback(
        code: str | None = None, state: str | None = None, error: str | None = None
    ) -> RedirectResponse:
        if auth is None:
            raise HTTPException(status_code=503, detail="login not configured")
        known = state in pending_states
        pending_states.discard(state or "")
        if error or not code or state is None or not known:
            return RedirectResponse("/?login=error", status_code=303)
        try:
            _user, raw = auth.complete_login(code)
        except Exception:  # bad code / exchange / profile failure -> surface in UI
            return RedirectResponse("/?login=error", status_code=303)
        resp = RedirectResponse("/", status_code=303)
        resp.set_cookie(
            _SESSION_COOKIE,
            raw,
            max_age=SESSION_TTL_S,
            httponly=True,
            secure=True,
            samesite="lax",
            path="/",
        )
        return resp

    @app.post("/api/auth/logout")
    def auth_logout(request: Request) -> RedirectResponse:
        raw = request.cookies.get(_SESSION_COOKIE) or ""
        if auth is not None and raw:
            auth.logout(raw)
        resp = RedirectResponse("/?login=out", status_code=303)
        resp.delete_cookie(_SESSION_COOKIE, path="/")
        return resp

    # health sits outside the authed router so proxy/uptime probes need no key
    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    if jobs is not None:
        api.include_router(jobs_router(jobs, manual_kinds=manual_job_kinds))
        mount_jobs_ws(app, jobs, _authenticate, poll_s=ws_poll_interval, ping_s=ws_ping_interval)
        if listening is not None and uploads_dir is not None:
            api.include_router(
                history_router(jobs, listening, uploads_dir, max_bytes=history_upload_max_bytes)
            )
        if sync is not None:
            api.include_router(
                sync_router(jobs, sync, interval_s=sync_interval_s, preview=sync_preview)
            )
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


def _user_dict(user: User) -> dict[str, Any]:
    """Serialise the logged-in user for ``/api/me`` and the console header."""
    return {
        "id": user.id,
        "spotify_id": user.spotify_id,
        "display_name": user.display_name,
        "email": user.email,
        "avatar_url": user.avatar_url,
        "is_admin": user.is_admin,
    }


def _identity_from_record(record: dict[str, Any]) -> TrackIdentity:
    """The wanted track identity from a stored request row (for match scoring)."""
    duration = record.get("duration_s")
    return TrackIdentity(
        artist=_clean(record.get("artist")),
        title=_clean(record.get("title")),
        isrc=_clean(record.get("isrc")),
        duration_s=int(duration) if duration is not None else None,
    )


def _candidate_row(
    index: int, c: Candidate, match: int, *, streamable: bool = False
) -> dict[str, Any]:
    """Serialise a candidate + its match % for the review UI. ``id`` is a stable
    per-response handle for radio selection; the full candidate is echoed back on
    download so the API caches nothing. ``streamable`` says the A/B player can
    audition it (a static preview clip or a proxyable source)."""
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
        "preview_url": c.preview_url,
        "art": c.art_url,
        "streamable": streamable,
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
        preview_url=_clean(body.get("preview_url")),
    )


def _live_frame(store: RequestStore) -> str:
    payload = {
        "stats": store.stats(),
        "in_flight": store.list_requests(status="downloading", limit=100),
        "feed": store.list_feed(limit=50),
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
