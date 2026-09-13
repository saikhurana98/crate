"""Library Sync HTTP routes (mounted under the authenticated ``/api`` router).

This PR adds the listening-history import. ``POST /api/history/upload`` streams
the raw request body (a zip or JSON Spotify export) to disk, with a size cap and
a server-generated filename, then queues a ``history_import`` job for the
uploading user. No multipart dependency is needed. ``GET /api/history/status``
reports that user's ledger.

``/api/sync/*`` serves the Sync page:
- the overview: tier cards, playlists, and last synced / next run / running job;
- tier members;
- auto-sync toggles for tiers and playlists, with baseline or backfill;
- missing-track counts, shown before a scope is switched on;
- previews: a 307 to a fresh ~30 s Deezer clip for a tier song.

Everything is per logged-in user.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from fastapi import APIRouter, Body, HTTPException, Query, Request
from fastapi.responses import RedirectResponse

from crate.adapters.inbound.ops_jobs import JobsPort
from crate.application.auth import SERVICE_USER_ID
from crate.application.library_sync import SYNC_MODES
from crate.domain.jobs import TERMINAL, JobKind

DEFAULT_UPLOAD_MAX_BYTES = 500 * 1024 * 1024


class ListeningPort(Protocol):
    def history_status(self, user_id: str) -> dict[str, Any]: ...


class SyncPort(Protocol):
    """The Sync page's reads + toggles (``LibrarySyncService``)."""

    def overview(self, user_id: str) -> dict[str, Any]: ...
    def tier_members(
        self, user_id: str, tier: int, *, limit: int, offset: int
    ) -> list[dict[str, Any]]: ...
    def set_tier_auto_sync(
        self, user_id: str, tier: int, on: bool, *, backfill: bool
    ) -> dict[str, Any]: ...
    def set_playlist_mode(
        self, user_id: str, playlist_id: str, mode: str, *, backfill: bool
    ) -> dict[str, Any] | None: ...
    def missing(self, user_id: str, scope: str) -> int: ...


def _logged_in(request: Request, action: str) -> str:
    uid = str(getattr(request.state, "user_id", SERVICE_USER_ID))
    if uid == SERVICE_USER_ID:
        raise HTTPException(status_code=400, detail=f"log in with Spotify to {action}")
    return uid


def history_router(
    jobs: JobsPort,
    listening: ListeningPort,
    uploads_dir: Path,
    *,
    max_bytes: int = DEFAULT_UPLOAD_MAX_BYTES,
) -> APIRouter:
    router = APIRouter()

    def _real_user(request: Request) -> str:
        return _logged_in(request, "import your listening history")

    @router.post("/history/upload")
    async def upload_history(request: Request) -> dict[str, Any]:
        uid = _real_user(request)
        uploads_dir.mkdir(parents=True, exist_ok=True)
        name = f"{uuid.uuid4().hex}.upload"  # never a client-supplied path
        dest = uploads_dir / name
        size = 0
        try:
            with dest.open("wb") as fh:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > max_bytes:
                        raise HTTPException(
                            status_code=413,
                            detail=f"upload exceeds {max_bytes // (1024 * 1024)} MB",
                        )
                    fh.write(chunk)
        except BaseException:
            dest.unlink(missing_ok=True)
            raise
        if size == 0:
            dest.unlink(missing_ok=True)
            raise HTTPException(status_code=400, detail="empty upload")
        job_id, _ = await asyncio.to_thread(
            jobs.enqueue,
            JobKind.HISTORY_IMPORT,
            triggered_by="manual",
            user_id=uid,
            params={"upload": name, "bytes": size},
        )
        return {"id": job_id, "bytes": size}

    @router.get("/history/status")
    def history_status(request: Request) -> dict[str, Any]:
        return listening.history_status(_real_user(request))

    return router


# TERMINAL holds JobStatus members; compare on their string values.
_TERMINAL = {str(getattr(s, "value", s)) for s in TERMINAL}
_ACTIVE = ("queued", "running")


def _brief(job: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "id",
        "status",
        "triggered_by",
        "created_at",
        "started_at",
        "finished_at",
        "summary",
        "error",
        "progress_done",
        "progress_total",
    )
    return {k: job.get(k) for k in keys}


PreviewFn = Callable[[str], "str | None"]


def sync_router(
    jobs: JobsPort,
    sync: SyncPort,
    *,
    interval_s: float,
    preview: PreviewFn | None = None,
) -> APIRouter:
    router = APIRouter()

    def _uid(request: Request) -> str:
        return _logged_in(request, "use Library Sync")

    def _schedule() -> dict[str, Any]:
        recent = jobs.list_jobs(kind=JobKind.LIBRARY_SYNC.value, limit=20)
        done = next((j for j in recent if j["status"] in _TERMINAL), None)
        running = next((j for j in recent if j["status"] in _ACTIVE), None)
        next_run = None
        if recent and interval_s > 0:
            last = datetime.fromisoformat(str(recent[0]["created_at"]))
            next_run = (last + timedelta(seconds=interval_s)).isoformat()
        return {
            "last_sync": _brief(done) if done else None,
            "running": _brief(running) if running else None,
            "next_run_at": next_run,
            "interval_s": interval_s,
        }

    def _tier(tier: int, allowed: tuple[int, ...]) -> int:
        if tier not in allowed:
            raise HTTPException(status_code=404, detail="no such tier")
        return tier

    @router.get("/sync/overview")
    def overview(request: Request) -> dict[str, Any]:
        return sync.overview(_uid(request)) | _schedule()

    @router.get("/sync/tiers/{tier}/members")
    def tier_members(
        request: Request,
        tier: int,
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
    ) -> list[dict[str, Any]]:
        uid = _uid(request)
        return sync.tier_members(uid, _tier(tier, (2, 3)), limit=limit, offset=offset)

    @router.put("/sync/tiers/{tier}")
    def set_tier(request: Request, tier: int, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        uid = _uid(request)
        on = body.get("auto_sync")
        if not isinstance(on, bool):
            raise HTTPException(status_code=400, detail="auto_sync must be true or false")
        backfill = bool(body.get("backfill", False))
        return sync.set_tier_auto_sync(uid, _tier(tier, (1, 2, 3)), on, backfill=backfill)

    @router.put("/sync/playlists/{playlist_id}")
    def set_playlist(
        request: Request, playlist_id: str, body: dict[str, Any] = Body(...)
    ) -> dict[str, Any]:
        uid = _uid(request)
        mode = str(body.get("mode", ""))
        if mode not in SYNC_MODES:
            raise HTTPException(status_code=400, detail=f"mode must be one of {SYNC_MODES}")
        res = sync.set_playlist_mode(
            uid, playlist_id, mode, backfill=bool(body.get("backfill", False))
        )
        if res is None:
            raise HTTPException(status_code=404, detail="unknown playlist")
        return res

    @router.get("/sync/preview/{spotify_id}")
    async def preview_clip(request: Request, spotify_id: str) -> RedirectResponse:
        """307 to the track's current Deezer preview. The URL is resolved server-side
        from the Spotify id and must be on Deezer's CDN, so this can't be used as an
        open redirect; it's fetched fresh because Deezer's clip URLs expire."""
        _uid(request)
        if preview is None:
            raise HTTPException(status_code=503, detail="previews not configured")
        try:
            url = await asyncio.to_thread(preview, spotify_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:  # Spotify/Deezer hiccup
            raise HTTPException(status_code=502, detail="preview lookup failed") from exc
        if not url:
            raise HTTPException(status_code=404, detail="no preview for this track")
        return RedirectResponse(url, status_code=307)

    @router.get("/sync/missing")
    def missing(request: Request, scope: str = Query(...)) -> dict[str, Any]:
        uid = _uid(request)
        try:
            return {"scope": scope, "missing": sync.missing(uid, scope)}
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    return router
