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
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles

from lbmd.composition.catalog import (
    KNOWN,
    PINNED,
    TIER_OF,
    LayoutError,
    default_layout,
    source_status,
    validate_layout,
)
from lbmd.infrastructure.sqlite.config_store import ConfigStore
from lbmd.infrastructure.sqlite.store import RequestStore

SourcesFn = Callable[[], list[dict[str, Any]]]


def create_app(
    store: RequestStore,
    *,
    api_key: str | None = None,
    static_dir: Path | None = None,
    sources: SourcesFn = source_status,
    config: ConfigStore | None = None,
    poll_interval: float = 2.0,
) -> FastAPI:
    app = FastAPI(title="lbmd console", docs_url=None, redoc_url=None, openapi_url=None)

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
