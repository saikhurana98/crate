"""Management-console HTTP API (read-only, Phase 1 — ADR-0018).

A FastAPI app over a read-only :class:`RequestStore`. The console process is a
separate reader on the same WAL DB the shim writes, so live updates are served
by polling the DB in the ``/api/events`` SSE stream — there is no cross-process
event bus.

Auth: an ``X-API-Key`` header, injected by the reverse proxy (Caddy) so the
browser never handles the key. ``/api/health`` is unauthenticated for probes.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles

from lbmd.composition.catalog import source_status
from lbmd.infrastructure.sqlite.store import RequestStore

SourcesFn = Callable[[], list[dict[str, Any]]]


def create_app(
    store: RequestStore,
    *,
    api_key: str | None = None,
    static_dir: Path | None = None,
    sources: SourcesFn = source_status,
    poll_interval: float = 2.0,
) -> FastAPI:
    app = FastAPI(title="lbmd console", docs_url=None, redoc_url=None, openapi_url=None)

    def require_key(x_api_key: str | None = Header(default=None)) -> None:
        if api_key and x_api_key != api_key:
            raise HTTPException(status_code=401, detail="unauthorized")

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
        return sources()

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


async def _event_stream(  # pragma: no cover - infinite streaming loop
    store: RequestStore, poll_interval: float
) -> AsyncIterator[str]:
    while True:
        payload = {
            "stats": store.stats(),
            "in_flight": store.list_requests(status="downloading", limit=100),
        }
        yield f"data: {json.dumps(payload)}\n\n"
        await asyncio.sleep(poll_interval)
