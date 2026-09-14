"""Deduplicate HTTP routes (mounted under the authenticated ``/api`` router).

Reads: the index and duplicate-group overview, the ranked groups, and a single
library file for the A/B ear test. "Ignore" only records that a group's copies are
intentionally different, so any logged-in user may do it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse

from crate.adapters.inbound.ops_jobs import JobsPort
from crate.application.auth import SERVICE_USER_ID
from crate.domain.jobs import JobKind


class DedupPort(Protocol):
    def overview(self) -> dict[str, Any]: ...
    def groups(
        self, *, limit: int = 50, offset: int = 0, variants: bool | None = None
    ) -> list[dict[str, Any]]: ...
    def ignore_group(self, group_id: str, user_id: str | None) -> int | None: ...
    def file_path(self, file_id: str) -> str | None: ...


def _last_scan(jobs: JobsPort | None) -> dict[str, Any] | None:
    if jobs is None:
        return None
    recent = jobs.list_jobs(kind=JobKind.DEDUP_SCAN.value, limit=1)
    if not recent:
        return None
    j = recent[0]
    keys = ("id", "status", "created_at", "finished_at", "progress_done", "progress_total")
    return {k: j.get(k) for k in keys} | {"summary": j.get("summary"), "error": j.get("error")}


def dedup_router(
    dedup: DedupPort, jobs: JobsPort | None = None, *, library_root: Path | None = None
) -> APIRouter:
    router = APIRouter()

    @router.get("/dedup/overview")
    def overview() -> dict[str, Any]:
        return dedup.overview() | {"last_scan": _last_scan(jobs)}

    @router.get("/dedup/groups")
    def groups(
        limit: int = Query(default=50, ge=1, le=200),
        offset: int = Query(default=0, ge=0),
        variants: bool | None = Query(default=None),
    ) -> list[dict[str, Any]]:
        return dedup.groups(limit=limit, offset=offset, variants=variants)

    @router.post("/dedup/groups/{group_id}/ignore")
    def ignore(request: Request, group_id: str) -> dict[str, Any]:
        uid = str(getattr(request.state, "user_id", SERVICE_USER_ID))
        pairs = dedup.ignore_group(group_id, uid)
        if pairs is None:
            raise HTTPException(status_code=404, detail="no such group")
        return {"id": group_id, "ignored_pairs": pairs}

    @router.get("/dedup/files/{file_id}/audio")
    def file_audio(file_id: str) -> FileResponse:
        """Stream one library file (HTTP Range, so <audio> can seek) for the A/B test.
        The path comes from the index, never the client, and must resolve inside the
        library root, so neither ``..`` nor a symlink can reach anything else."""
        rel = dedup.file_path(file_id)
        if library_root is None or rel is None:
            raise HTTPException(status_code=404, detail="no such file")
        root = library_root.resolve()
        path = (root / rel).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise HTTPException(status_code=404, detail="no such file")
        return FileResponse(str(path))

    return router
