"""Jobs REST (the Library Sync / Dedup engine): list and inspect jobs, replay a
job's log, trigger an allow-listed kind, cancel.

Mounted under the authenticated ``/api`` router by :func:`create_app`. The live
tail is the WebSocket hub (``/api/ws/jobs``, next PR); these endpoints are its
REST fallback and the history view. Job metadata is shared, but log lines tagged
with a user are only returned to that user.
"""

from __future__ import annotations

from typing import Any, Protocol

from fastapi import APIRouter, Body, HTTPException, Query, Request

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
