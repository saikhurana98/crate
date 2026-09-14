"""Purge, restore and empty the library trash (the ``mutate`` lane, serve only).

**Purge** checks each copy again right before touching it (see
:func:`crate.domain.purge.refuse_reason`). For a copy that passes:

1. write a ``pending`` journal row;
2. rename the file into ``.crate-trash/<day>/<path>``. This is a same-filesystem
   rename; a cross-device move stops the job rather than copying;
3. in one transaction: repoint download records and in-flight upgrade targets
   at the keeper (copying its quality columns), mark the index row missing, and
   mark the journal row ``trashed``.

If step 3 fails, the file is renamed back.

**Restore** renames a trashed file back to its original path, or to
``… (restored-N)`` if that path is taken. **Empty** deletes trashed files for
good: all of them on request, else those older than the retention window.

Every job first reconciles journal rows a crash left ``pending``: a file found in
the trash is moved back, and the row is marked failed.
"""

from __future__ import annotations

import errno
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta
from functools import partial
from typing import Any, Protocol

from crate.application.jobs import JobContext
from crate.domain.purge import PurgeItem, free_path, refuse_reason, trash_relpath


class LibraryFs(Protocol):
    def stat(self, rel: str) -> tuple[int, int] | None: ...  # (size, mtime_ns)
    def exists(self, rel: str) -> bool: ...
    def move(self, src: str, dst: str) -> None: ...  # same-filesystem rename
    def unlink(self, rel: str) -> None: ...
    def ensure_trash_root(self) -> None: ...


class TrashStore(Protocol):
    def file_row(self, file_id: str) -> dict[str, Any] | None: ...
    def abs_path(self, rel: str) -> str: ...
    def inflight_paths(self) -> set[str]: ...
    def path_taken(self, rel: str, except_id: str) -> bool: ...
    def begin(
        self, item: PurgeItem, original: str, trash_rel: str, job_id: str, user_id: str | None
    ) -> str: ...
    def commit_purge(self, trash_id: str, item: PurgeItem, keeper: dict[str, Any]) -> None: ...
    def fail(self, trash_id: str, error: str) -> None: ...
    def pending(self) -> list[dict[str, Any]]: ...
    def trashed(
        self, ids: Iterable[str] | None = None, older_than: str | None = None
    ) -> list[dict[str, Any]]: ...
    def commit_restore(self, trash_id: str, file_id: str, restored_rel: str) -> None: ...
    def mark_purged(self, trash_id: str) -> None: ...


def reconcile_pending(ctx: JobContext, store: TrashStore, fs: LibraryFs) -> int:
    """Settle journal rows a crash left ``pending``; returns how many."""
    rows = store.pending()
    for row in rows:
        original, trash = str(row["original_path"]), str(row["trash_path"])
        if fs.exists(trash) and not fs.exists(original):
            fs.move(trash, original)
            store.fail(str(row["id"]), "interrupted by a restart; moved back")
        else:
            store.fail(str(row["id"]), "interrupted by a restart")
        ctx.warn(f"recovered an interrupted purge of {original}", code="purge_recovered")
    return len(rows)


def _check(item: PurgeItem, store: TrashStore, fs: LibraryFs, inflight: set[str]) -> str | None:
    row = store.file_row(item.file_id)
    keeper = store.file_row(item.keeper_id)
    return refuse_reason(
        item,
        file_row=row,
        keeper_row=keeper,
        on_disk=fs.stat(str(row["path"])) if row else None,
        keeper_on_disk=keeper is not None and fs.stat(str(keeper["path"])) is not None,
        inflight=row is not None and store.abs_path(str(row["path"])) in inflight,
    )


def run_purge(
    ctx: JobContext,
    items: list[PurgeItem],
    *,
    store: TrashStore,
    fs: LibraryFs,
    day: str,
    user_id: str | None = None,
    rescan: Callable[[], None] | None = None,
) -> dict[str, Any]:
    reconcile_pending(ctx, store, fs)
    fs.ensure_trash_root()
    inflight = store.inflight_paths()
    trashed = skipped = failed = freed = 0
    for n, item in enumerate(items, 1):
        ctx.check_cancelled()
        reason = _check(item, store, fs, inflight)
        row = store.file_row(item.file_id)
        label = str(row["path"]) if row else item.file_id
        if reason is not None or row is None:
            skipped += 1
            ctx.warn(f"kept {label}: {reason}", code="purge_skipped")
            continue
        keeper = store.file_row(item.keeper_id)
        assert keeper is not None  # _check refused otherwise
        original = str(row["path"])
        dest = free_path(trash_relpath(original, day), fs.exists)
        trash_id = store.begin(item, original, dest, ctx.job.id, user_id)
        try:
            fs.move(original, dest)
        except OSError as exc:
            failed += 1
            store.fail(trash_id, str(exc))
            if exc.errno == errno.EXDEV:
                ctx.error(
                    "the trash is on a different filesystem; stopping (files are never "
                    "copied and deleted)",
                    code="cross_device",
                )
                break
            ctx.warn(f"couldn't move {original}: {exc}", code="purge_failed")
            continue
        try:
            store.commit_purge(trash_id, item, keeper)
        except Exception as exc:
            fs.move(dest, original)  # the DB didn't follow; put the file back
            failed += 1
            store.fail(trash_id, f"database update failed: {exc}")
            ctx.error(
                f"couldn't record the purge of {original}; moved it back", code="purge_failed"
            )
            continue
        trashed += 1
        freed += item.size
        ctx.log(f"trashed {original}", code="trashed", data={"file_id": item.file_id})
        ctx.progress(n, len(items), f"trashing {n}/{len(items)}")
    if trashed and rescan is not None:
        try:
            rescan()
        except Exception as exc:  # the purge stands; Navidrome catches up on its own scan
            ctx.warn(f"Navidrome rescan failed: {exc}", code="rescan_failed")
    ctx.log(
        f"trashed {trashed} file(s) ({freed / 1e6:.1f} MB), kept {skipped}, {failed} failed",
        code="summary",
    )
    return {"trashed": trashed, "skipped": skipped, "failed": failed, "freed_bytes": freed}


def _restore_taken(fs: LibraryFs, store: TrashStore, file_id: str, rel: str) -> bool:
    """A restore target is taken if something is on disk there, or another index
    row already claims that path."""
    return fs.exists(rel) or store.path_taken(rel, file_id)


def run_restore(
    ctx: JobContext,
    ids: list[str],
    *,
    store: TrashStore,
    fs: LibraryFs,
    rescan: Callable[[], None] | None = None,
) -> dict[str, Any]:
    reconcile_pending(ctx, store, fs)
    restored = failed = 0
    for row in store.trashed(ids):
        ctx.check_cancelled()
        trash_id, file_id = str(row["id"]), str(row["file_id"])
        trash, original = str(row["trash_path"]), str(row["original_path"])
        if not fs.exists(trash):
            failed += 1
            store.fail(trash_id, "the file is no longer in the trash")
            ctx.warn(f"{original} is no longer in the trash", code="restore_failed")
            continue
        dest = free_path(original, partial(_restore_taken, fs, store, file_id))
        fs.move(trash, dest)
        store.commit_restore(trash_id, file_id, dest)
        restored += 1
        ctx.log(f"restored {dest}", code="restored")
    if restored and rescan is not None:
        try:
            rescan()
        except Exception as exc:
            ctx.warn(f"Navidrome rescan failed: {exc}", code="rescan_failed")
    ctx.log(f"restored {restored} file(s), {failed} failed", code="summary")
    return {"restored": restored, "failed": failed}


def run_empty(
    ctx: JobContext,
    *,
    store: TrashStore,
    fs: LibraryFs,
    everything: bool,
    retention_days: int,
    now: datetime,
) -> dict[str, Any]:
    reconcile_pending(ctx, store, fs)
    cutoff = None if everything else (now - timedelta(days=retention_days)).isoformat()
    purged = freed = 0
    for row in store.trashed(older_than=cutoff):
        ctx.check_cancelled()
        fs.unlink(str(row["trash_path"]))
        store.mark_purged(str(row["id"]))
        purged += 1
        freed += int(row["size"])
    scope = "everything" if everything else f"older than {retention_days} days"
    ctx.log(f"emptied the trash ({scope}): {purged} file(s), {freed / 1e6:.1f} MB", code="summary")
    return {"purged": purged, "freed_bytes": freed}
