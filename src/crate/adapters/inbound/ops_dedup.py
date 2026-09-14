"""Deduplicate HTTP routes (mounted under the authenticated ``/api`` router).

Read-only for now: the index and duplicate-group overview, and the ranked groups.
The one write, "ignore", only records that a group's copies are intentionally
different, so any logged-in user may do it. Destructive actions (purge, trash)
arrive with their own admin gate.
"""

from __future__ import annotations

from typing import Any, Protocol

from fastapi import APIRouter, HTTPException, Query, Request

from crate.adapters.inbound.ops_jobs import JobsPort
from crate.application.auth import SERVICE_USER_ID
from crate.domain.jobs import JobKind


class DedupPort(Protocol):
    def overview(self) -> dict[str, Any]: ...
    def groups(
        self, *, limit: int = 50, offset: int = 0, variants: bool | None = None
    ) -> list[dict[str, Any]]: ...
    def ignore_group(self, group_id: str, user_id: str | None) -> int | None: ...


def _last_scan(jobs: JobsPort | None) -> dict[str, Any] | None:
    if jobs is None:
        return None
    recent = jobs.list_jobs(kind=JobKind.DEDUP_SCAN.value, limit=1)
    if not recent:
        return None
    j = recent[0]
    keys = ("id", "status", "created_at", "finished_at", "progress_done", "progress_total")
    return {k: j.get(k) for k in keys} | {"summary": j.get("summary"), "error": j.get("error")}


def dedup_router(dedup: DedupPort, jobs: JobsPort | None = None) -> APIRouter:
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

    return router
