"""SQLite side of purge / restore / empty: the trash journal plus the repointing of
download records and in-flight upgrade targets, which happens in one transaction
with the journal update."""

from __future__ import annotations

import sqlite3
import threading
import uuid
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from crate.domain.audio import AudioInfo, classify
from crate.domain.purge import PurgeItem
from crate.infrastructure.sqlite.db import now_iso


def _quality_label(row: dict[str, Any]) -> str:
    info = AudioInfo(
        codec=str(row.get("codec") or "unknown"),
        container=str(row.get("container") or "unknown"),
        sample_rate_hz=int(row.get("sample_rate") or 0),
        channels=int(row.get("channels") or 0),
        bit_depth=row.get("bit_depth"),
        bitrate_bps=row.get("bitrate"),
        duration_s=float(row.get("duration_s") or 0.0),
        size_bytes=int(row.get("size") or 0),
    )
    return classify(info).label


class SqliteTrashStore:
    def __init__(
        self, conn: sqlite3.Connection, root: Path, *, lock: threading.Lock | None = None
    ) -> None:
        self._conn = conn
        self._root = root
        self._lock = lock or threading.Lock()

    def abs_path(self, rel: str) -> str:
        return str(self._root / rel)

    def file_row(self, file_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM library_files WHERE id=? AND missing_since IS NULL", (file_id,)
            ).fetchone()
        return dict(row) if row else None

    def inflight_paths(self) -> set[str]:
        """Files a queued or running review upgrade is about to replace."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT rc.target_path FROM review_candidates rc "
                "JOIN requests r ON r.id = rc.request_id "
                "WHERE rc.target_path IS NOT NULL AND r.status IN ('queued', 'downloading')"
            ).fetchall()
        return {str(r[0]) for r in rows}

    def path_taken(self, rel: str, except_id: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM library_files WHERE path=? AND id<>?", (rel, except_id)
            ).fetchone()
        return row is not None

    def begin(
        self, item: PurgeItem, original: str, trash_rel: str, job_id: str, user_id: str | None
    ) -> str:
        trash_id = uuid.uuid4().hex
        with self._lock:
            self._conn.execute(
                "INSERT INTO trash_items(id, file_id, keeper_file_id, group_id, original_path, "
                "trash_path, size, state, job_id, user_id, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)",
                (
                    trash_id,
                    item.file_id,
                    item.keeper_id,
                    item.group_id,
                    original,
                    trash_rel,
                    item.size,
                    job_id,
                    user_id,
                    now_iso(),
                ),
            )
        return trash_id

    def commit_purge(self, trash_id: str, item: PurgeItem, keeper: dict[str, Any]) -> None:
        """One transaction: downloads + upgrade targets -> keeper (with its quality),
        index row missing, journal trashed, group membership trimmed."""
        now = now_iso()
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT original_path FROM trash_items WHERE id=?", (trash_id,)
            ).fetchone()
            old_abs = self.abs_path(str(row[0]))
            new_abs = self.abs_path(str(keeper["path"]))
            self._conn.execute(
                "UPDATE downloads SET path=?, size_bytes=?, codec=?, bitrate=?, bit_depth=?, "
                "sample_rate=?, duration_s=?, lossless=?, quality_label=? WHERE path=?",
                (
                    new_abs,
                    keeper.get("size") or 0,
                    keeper.get("codec") or "unknown",
                    keeper.get("bitrate"),
                    keeper.get("bit_depth"),
                    keeper.get("sample_rate"),
                    keeper.get("duration_s"),
                    1 if keeper.get("lossless") else 0,
                    _quality_label(keeper),
                    old_abs,
                ),
            )
            self._conn.execute(
                "UPDATE review_candidates SET target_path=? WHERE target_path=?",
                (new_abs, old_abs),
            )
            self._conn.execute(
                "UPDATE library_files SET missing_since=? WHERE id=?", (now, item.file_id)
            )
            self._conn.execute(
                "UPDATE trash_items SET state='trashed', trashed_at=? WHERE id=?",
                (now, trash_id),
            )
            groups = [
                str(r[0])
                for r in self._conn.execute(
                    "SELECT group_id FROM dup_members WHERE file_id=?", (item.file_id,)
                )
            ]
            self._conn.execute("DELETE FROM dup_members WHERE file_id=?", (item.file_id,))
            for gid in groups:
                left = self._conn.execute(
                    "SELECT COUNT(*) FROM dup_members WHERE group_id=?", (gid,)
                ).fetchone()[0]
                if left <= 1:
                    self._conn.execute("DELETE FROM dup_members WHERE group_id=?", (gid,))
                    self._conn.execute("DELETE FROM dup_groups WHERE id=?", (gid,))
                else:
                    self._conn.execute(
                        "UPDATE dup_groups SET reclaimable_bytes=MAX(0, reclaimable_bytes-?) "
                        "WHERE id=?",
                        (item.size, gid),
                    )

    def fail(self, trash_id: str, error: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE trash_items SET state='failed', error=? WHERE id=?", (error, trash_id)
            )

    def pending(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM trash_items WHERE state='pending'")
            return [dict(r) for r in rows]

    def trashed(
        self, ids: Iterable[str] | None = None, older_than: str | None = None
    ) -> list[dict[str, Any]]:
        sql, params = "SELECT * FROM trash_items WHERE state='trashed'", []
        wanted = list(ids) if ids is not None else None
        if wanted is not None:
            if not wanted:
                return []
            sql += f" AND id IN ({','.join('?' * len(wanted))})"
            params += wanted
        if older_than is not None:
            sql += " AND trashed_at < ?"
            params.append(older_than)
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql + " ORDER BY trashed_at", params)]

    def commit_restore(self, trash_id: str, file_id: str, restored_rel: str) -> None:
        """The file is back: live again at ``restored_rel``. Its analysis time is
        bumped so the next grouping compares it again."""
        now = now_iso()
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE library_files SET path=?, missing_since=NULL, analyzed_at=?, "
                "scanned_at=? WHERE id=?",
                (restored_rel, now, now, file_id),
            )
            self._conn.execute(
                "UPDATE trash_items SET state='restored', restored_path=?, restored_at=? "
                "WHERE id=?",
                (restored_rel, now, trash_id),
            )

    def mark_purged(self, trash_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE trash_items SET state='purged', purged_at=? WHERE id=?",
                (now_iso(), trash_id),
            )
