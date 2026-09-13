"""Library Sync HTTP routes (mounted under the authenticated ``/api`` router).

This PR adds the listening-history import. ``POST /api/history/upload`` streams
the raw request body (a zip or JSON Spotify export) to disk, with a size cap and
a server-generated filename, then queues a ``history_import`` job for the
uploading user. No multipart dependency is needed. ``GET /api/history/status``
reports that user's ledger. Tier and playlist sync routes land here in later PRs.
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Any, Protocol

from fastapi import APIRouter, HTTPException, Request

from crate.adapters.inbound.ops_jobs import JobsPort
from crate.application.auth import SERVICE_USER_ID
from crate.domain.jobs import JobKind

DEFAULT_UPLOAD_MAX_BYTES = 500 * 1024 * 1024


class ListeningPort(Protocol):
    def history_status(self, user_id: str) -> dict[str, Any]: ...


def history_router(
    jobs: JobsPort,
    listening: ListeningPort,
    uploads_dir: Path,
    *,
    max_bytes: int = DEFAULT_UPLOAD_MAX_BYTES,
) -> APIRouter:
    router = APIRouter()

    def _real_user(request: Request) -> str:
        uid = str(getattr(request.state, "user_id", SERVICE_USER_ID))
        if uid == SERVICE_USER_ID:
            raise HTTPException(
                status_code=400, detail="log in with Spotify to import your listening history"
            )
        return uid

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
