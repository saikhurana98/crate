"""Jobs REST + live log (the Library Sync / Dedup engine).

REST (mounted under the authenticated ``/api`` router): list and inspect jobs,
replay a job's log, trigger an allow-listed kind, cancel. **WebSocket**
``/api/ws/jobs``: a backlog then a live tail of job events (ADR-0020). The
cross-process transport is still the shared DB (ADR-0018): each socket tails
``job_events`` by id cursor. Job metadata is shared; log lines tagged with a user
reach only that user.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable, Mapping
from typing import Any, Protocol
from urllib.parse import urlparse

from fastapi import APIRouter, Body, FastAPI, HTTPException, Query, Request, WebSocket
from starlette.requests import HTTPConnection
from starlette.websockets import WebSocketDisconnect

from crate.application.auth import SERVICE_USER_ID
from crate.domain.jobs import JobKind


class JobsPort(Protocol):
    def enqueue(
        self,
        kind: JobKind,
        *,
        triggered_by: str,
        user_id: str | None = None,
        params: dict[str, Any] | None = None,
    ) -> tuple[str, bool]: ...

    def get(self, job_id: str) -> dict[str, Any] | None: ...

    def list_jobs(self, *, kind: str | None = None, limit: int = 50) -> list[dict[str, Any]]: ...

    def events(
        self, job_id: str, *, after: int = 0, limit: int = 500, viewer: str | None = None
    ) -> list[dict[str, Any]]: ...

    def request_cancel(self, job_id: str) -> str | None: ...

    def events_since(
        self, cursor: int, *, limit: int = 500, viewer: str | None = None
    ) -> list[dict[str, Any]]: ...

    def tail_events(
        self, *, limit: int = 200, viewer: str | None = None
    ) -> list[dict[str, Any]]: ...

    def max_event_id(self) -> int: ...

    def active_jobs(self) -> list[dict[str, Any]]: ...


def jobs_router(jobs: JobsPort, *, manual_kinds: frozenset[JobKind]) -> APIRouter:
    """REST routes over ``jobs``. Only ``manual_kinds`` can be triggered by hand
    (a kind without a registered handler is never in that set)."""
    router = APIRouter()

    def _uid(request: Request) -> str:
        return str(getattr(request.state, "user_id", SERVICE_USER_ID))

    @router.get("/jobs")
    def list_jobs(
        kind: str | None = None, limit: int = Query(default=50, ge=1, le=200)
    ) -> list[dict[str, Any]]:
        return jobs.list_jobs(kind=kind, limit=limit)

    @router.post("/jobs")
    def trigger_job(request: Request, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        raw = str(body.get("kind", "")).strip()
        try:
            kind = JobKind(raw)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="unknown job kind") from exc
        if kind not in manual_kinds:
            raise HTTPException(status_code=400, detail=f"{raw} can't be triggered manually")
        job_id, existing = jobs.enqueue(kind, triggered_by="manual", user_id=_uid(request))
        return {"id": job_id, "existing": existing}

    @router.get("/jobs/{job_id}")
    def job_detail(job_id: str) -> dict[str, Any]:
        job = jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="job not found")
        return job

    @router.get("/jobs/{job_id}/events")
    def job_events(
        request: Request,
        job_id: str,
        after: int = Query(default=0, ge=0),
        limit: int = Query(default=500, ge=1, le=1000),
    ) -> list[dict[str, Any]]:
        if jobs.get(job_id) is None:
            raise HTTPException(status_code=404, detail="job not found")
        return jobs.events(job_id, after=after, limit=limit, viewer=_uid(request))

    @router.post("/jobs/{job_id}/cancel")
    def cancel_job(job_id: str) -> dict[str, Any]:
        if jobs.get(job_id) is None:
            raise HTTPException(status_code=404, detail="job not found")
        status = jobs.request_cancel(job_id)
        if status is None:
            raise HTTPException(status_code=409, detail="job already finished")
        return {"id": job_id, "status": status}

    return router


# --- WebSocket live log (ADR-0020) ---
WS_CLOSE_UNAUTHORIZED = 4401
WS_CLOSE_BAD_ORIGIN = 4403
_WS_BACKLOG = 200  # events replayed on a fresh connect
_WS_BATCH = 500  # max events per live frame

Authenticate = Callable[[HTTPConnection], "str | None"]


def origin_allowed(headers: Mapping[str, str]) -> bool:
    """Same-origin check on the WebSocket upgrade (the cross-site WebSocket
    hijacking guard). Browsers always send ``Origin``, and it must match ``Host``.
    Non-browser clients (no ``Origin``) pass; they still need a session or the key."""
    origin = headers.get("origin")
    if not origin:
        return True
    return urlparse(origin).netloc.lower() == (headers.get("host") or "").lower()


def job_frame(
    events: list[dict[str, Any]], cursor: int, active: list[dict[str, Any]]
) -> dict[str, Any]:
    """One server->client frame: new events, the resume cursor, and the
    currently queued/running jobs (so the UI can show a running sync)."""
    return {"type": "events", "events": events, "cursor": cursor, "active": active}


def mount_jobs_ws(
    app: FastAPI,
    jobs: JobsPort,
    authenticate: Authenticate,
    *,
    poll_s: float = 1.0,
    ping_s: float = 25.0,
) -> None:
    """Register ``/api/ws/jobs`` on ``app`` (not the ``/api`` router: HTTP
    dependencies don't apply to WebSocket routes, so auth is checked here).

    Protocol: connect (optionally ``?since=<event id>`` to resume), receive one
    backlog frame, then a frame whenever new events land, plus a ``ping`` after
    ``ping_s`` of silence. Close codes: 4401 unauthenticated, 4403 cross-origin."""

    @app.websocket("/api/ws/jobs")
    async def jobs_ws(ws: WebSocket) -> None:
        await ws.accept()
        if not origin_allowed(ws.headers):
            await ws.close(code=WS_CLOSE_BAD_ORIGIN)
            return
        uid = authenticate(ws)
        if uid is None:
            await ws.close(code=WS_CLOSE_UNAUTHORIZED)
            return

        async def until_disconnect() -> None:
            # Drains client messages (ignored) so a disconnect is noticed within one
            # poll, instead of only at the next send.
            with contextlib.suppress(Exception):
                while (await ws.receive())["type"] != "websocket.disconnect":
                    pass

        watcher = asyncio.create_task(until_disconnect())
        try:
            since = ws.query_params.get("since", "")
            if since.isdigit():
                cursor = int(since)
                rows = await asyncio.to_thread(
                    jobs.events_since, cursor, limit=_WS_BATCH, viewer=uid
                )
            else:
                rows = await asyncio.to_thread(jobs.tail_events, limit=_WS_BACKLOG, viewer=uid)
                cursor = 0
            if rows:
                cursor = int(rows[-1]["id"])
            elif not since.isdigit():
                cursor = await asyncio.to_thread(jobs.max_event_id)
            active = await asyncio.to_thread(jobs.active_jobs)
            await ws.send_json(job_frame(rows, cursor, active))
            idle = 0.0
            while not watcher.done():
                await asyncio.sleep(poll_s)
                rows = await asyncio.to_thread(
                    jobs.events_since, cursor, limit=_WS_BATCH, viewer=uid
                )
                if rows:
                    cursor = int(rows[-1]["id"])
                    active = await asyncio.to_thread(jobs.active_jobs)
                    await ws.send_json(job_frame(rows, cursor, active))
                    idle = 0.0
                    continue
                idle += poll_s
                if idle >= ping_s:
                    await ws.send_json({"type": "ping", "cursor": cursor})
                    idle = 0.0
        except (WebSocketDisconnect, RuntimeError):  # client went away mid-send
            return
        finally:
            watcher.cancel()
