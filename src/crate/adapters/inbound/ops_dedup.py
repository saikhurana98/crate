"""Deduplicate HTTP routes (mounted under the authenticated ``/api`` router).

Reads: the index and duplicate-group overview, the ranked groups, the trash, and
a single library file for the A/B ear test. "Ignore" only records that a group's
copies are intentionally different, so any logged-in user may do it.

Everything that moves or deletes files is **admin only** (403 otherwise):
- trash a group's other copies;
- trash every byte-identical copy;
- restore from the trash;
- empty the trash.

These routes only queue jobs; ``serve``, which owns the read-write library
mount, does the work and checks every copy again first.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from fastapi import APIRouter, Body, HTTPException, Query, Request
from fastapi.responses import FileResponse

from crate.adapters.inbound.ops_jobs import JobsPort
from crate.application.auth import SERVICE_USER_ID
from crate.domain.jobs import JobKind
from crate.domain.purge import PurgeItem

_MUTATING = {JobKind.DEDUP_PURGE.value, JobKind.TRASH_RESTORE.value, JobKind.TRASH_EMPTY.value}


class DedupPort(Protocol):
    def overview(self) -> dict[str, Any]: ...
    def groups(
        self, *, limit: int = 50, offset: int = 0, variants: bool | None = None
    ) -> list[dict[str, Any]]: ...
    def ignore_group(self, group_id: str, user_id: str | None) -> int | None: ...
    def file_path(self, file_id: str) -> str | None: ...
    def group(self, group_id: str) -> dict[str, Any] | None: ...
    def exact_groups(self) -> list[dict[str, Any]]: ...
    def trash(self, limit: int = 200) -> dict[str, Any]: ...


def _last_scan(jobs: JobsPort | None) -> dict[str, Any] | None:
    if jobs is None:
        return None
    recent = jobs.list_jobs(kind=JobKind.DEDUP_SCAN.value, limit=1)
    if not recent:
        return None
    j = recent[0]
    keys = ("id", "status", "created_at", "finished_at", "progress_done", "progress_total")
    return {k: j.get(k) for k in keys} | {"summary": j.get("summary"), "error": j.get("error")}


def _items(group: dict[str, Any], keep: str, trash: list[str]) -> list[dict[str, Any]]:
    """What the user saw for each copy (size + mtime), so serve can refuse a copy
    that changed in between."""
    members = {str(m["id"]): m for m in group["members"]}
    return [
        PurgeItem(
            fid, keep, str(group["id"]), int(members[fid]["size"]), int(members[fid]["mtime_ns"])
        ).to_dict()
        for fid in trash
    ]


def dedup_router(
    dedup: DedupPort,
    jobs: JobsPort | None = None,
    *,
    library_root: Path | None = None,
    is_admin: Callable[[Request], bool] | None = None,
) -> APIRouter:
    router = APIRouter()

    def _admin(request: Request) -> str:
        if is_admin is None or not is_admin(request):
            raise HTTPException(status_code=403, detail="only an admin can move or delete files")
        return str(getattr(request.state, "user_id", SERVICE_USER_ID))

    def _jobs() -> JobsPort:
        if jobs is None:
            raise HTTPException(status_code=503, detail="background jobs are not configured")
        return jobs

    @router.get("/dedup/overview")
    def overview() -> dict[str, Any]:
        mutating = jobs is not None and any(j["kind"] in _MUTATING for j in jobs.active_jobs())
        return dedup.overview() | {"last_scan": _last_scan(jobs), "mutating": mutating}

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

    @router.post("/dedup/groups/{group_id}/purge")
    def purge(
        request: Request, group_id: str, body: dict[str, Any] = Body(default={})
    ) -> dict[str, Any]:
        """Keep one copy (default: the recommended keeper), trash the others (default:
        all of them; a variant group needs an explicit list)."""
        uid = _admin(request)
        queue = _jobs()
        group = dedup.group(group_id)
        if group is None:
            raise HTTPException(status_code=404, detail="no such group")
        ids = [str(m["id"]) for m in group["members"]]
        keep = str(body.get("keep") or group["keeper_file_id"])
        if keep not in ids:
            raise HTTPException(status_code=400, detail="keep must be one of the group's copies")
        chosen = body.get("trash")
        if chosen is None:
            if group["variant"]:
                raise HTTPException(
                    status_code=400, detail="variants need an explicit list of copies to trash"
                )
            chosen = [i for i in ids if i != keep]
        trash = [str(t) for t in chosen] if isinstance(chosen, list) else []
        if not trash or keep in trash or any(t not in ids for t in trash):
            raise HTTPException(
                status_code=400, detail="trash must be other copies from this group"
            )
        items = _items(group, keep, trash)
        job_id, _ = queue.enqueue(
            JobKind.DEDUP_PURGE, triggered_by="manual", user_id=uid, params={"items": items}
        )
        return {"job": job_id, "files": len(items), "bytes": sum(i["size"] for i in items)}

    @router.post("/dedup/resolve-exact")
    def resolve_exact(request: Request) -> dict[str, Any]:
        """Trash every copy that is byte-for-byte identical to the copy kept."""
        uid = _admin(request)
        queue = _jobs()
        items = [
            item
            for g in dedup.exact_groups()
            for item in _items(
                g, str(g["keeper_file_id"]), [str(m["id"]) for m in g["members"] if m["rank"] > 0]
            )
        ]
        if not items:
            raise HTTPException(status_code=404, detail="no byte-identical copies")
        job_id, _ = queue.enqueue(
            JobKind.DEDUP_PURGE, triggered_by="manual", user_id=uid, params={"items": items}
        )
        return {"job": job_id, "files": len(items), "bytes": sum(i["size"] for i in items)}

    @router.get("/dedup/trash")
    def trash(limit: int = Query(default=200, ge=1, le=1000)) -> dict[str, Any]:
        return dedup.trash(limit)

    @router.post("/dedup/trash/restore")
    def restore(request: Request, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        uid = _admin(request)
        ids = body.get("ids")
        if not isinstance(ids, list) or not ids:
            raise HTTPException(status_code=400, detail="ids must be a non-empty list")
        job_id, _ = _jobs().enqueue(
            JobKind.TRASH_RESTORE,
            triggered_by="manual",
            user_id=uid,
            params={"ids": [str(i) for i in ids]},
        )
        return {"job": job_id, "files": len(ids)}

    @router.post("/dedup/trash/empty")
    def empty(request: Request, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """Delete everything in the trash for good. The body must carry
        ``{"confirm": "EMPTY"}``."""
        uid = _admin(request)
        if body.get("confirm") != "EMPTY":
            raise HTTPException(status_code=400, detail='type "EMPTY" to confirm')
        job_id, existing = _jobs().enqueue(
            JobKind.TRASH_EMPTY, triggered_by="manual", user_id=uid, params={"all": True}
        )
        return {"job": job_id, "existing": existing}

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
