"""SQLite storage for the library index (``library_files``) behind the dedup scan."""

from __future__ import annotations

import sqlite3
import threading
import uuid
from typing import Any

from crate.application.identify import AcoustidMatch
from crate.application.library_scan import FileAnalysis
from crate.domain.chromaprint import pack_raw
from crate.domain.library_scan import IndexedFile, ScanPlan
from crate.domain.listening import merge_key
from crate.infrastructure.sqlite.db import now_iso


class SqliteLibraryStore:
    def __init__(self, conn: sqlite3.Connection, *, lock: threading.Lock | None = None) -> None:
        self._conn = conn
        self._lock = lock or threading.Lock()

    def indexed(self) -> list[IndexedFile]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, path, size, mtime_ns, inode, missing_since IS NOT NULL AS gone "
                "FROM library_files"
            ).fetchall()
        return [
            IndexedFile(r["id"], r["path"], r["size"], r["mtime_ns"], r["inode"], bool(r["gone"]))
            for r in rows
        ]

    def apply_plan(self, plan: ScanPlan) -> None:
        """Write one walk's outcome in a single transaction."""
        now = now_iso()
        with self._lock, self._conn:
            self._conn.executemany(
                "INSERT INTO library_files(id, path, size, mtime_ns, inode, first_seen_at, "
                "scanned_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    (uuid.uuid4().hex, e.path, e.size, e.mtime_ns, e.inode, now, now)
                    for e in plan.new
                ],
            )
            self._conn.executemany(
                "UPDATE library_files SET size=?, mtime_ns=?, inode=?, scanned_at=?, "
                "missing_since=NULL, analyzed_at=NULL, looked_up_at=NULL, content_hash=NULL, "
                "error=NULL WHERE id=?",
                [(e.size, e.mtime_ns, e.inode, now, fid) for fid, e in plan.changed],
            )
            self._conn.executemany(
                "UPDATE library_files SET path=?, inode=?, scanned_at=?, missing_since=NULL "
                "WHERE id=?",
                [(e.path, e.inode, now, fid) for fid, e in plan.moved],
            )
            self._conn.executemany(
                "UPDATE library_files SET scanned_at=?, missing_since=NULL WHERE id=?",
                [(now, fid) for fid, _ in plan.seen],
            )
            self._conn.executemany(
                "UPDATE library_files SET missing_since=? WHERE id=?",
                [(now, fid) for fid in plan.missing],
            )

    def pending_analysis(self) -> list[tuple[str, str]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, path FROM library_files WHERE analyzed_at IS NULL "
                "AND missing_since IS NULL ORDER BY path"
            ).fetchall()
        return [(r["id"], r["path"]) for r in rows]

    def save_analysis(self, file_id: str, a: FileAnalysis) -> None:
        t = a.tags
        with self._lock:
            self._conn.execute(
                "UPDATE library_files SET codec=?, container=?, lossless=?, bitrate=?, "
                "bit_depth=?, sample_rate=?, channels=?, duration_s=?, artist=?, album=?, "
                "title=?, album_artist=?, track_no=?, disc_no=?, tag_key=?, has_cover=?, "
                "fp_duration=?, fp_compressed=?, fp_raw=?, analyzed_at=?, looked_up_at=NULL, "
                "error=NULL WHERE id=?",
                (
                    a.codec,
                    a.container,
                    1 if a.lossless else 0,
                    a.bitrate,
                    a.bit_depth,
                    a.sample_rate,
                    a.channels,
                    a.duration_s,
                    t.artist,
                    t.album,
                    t.title,
                    t.album_artist,
                    t.track_no,
                    t.disc_no,
                    merge_key(t.artist, t.title) if (t.artist or t.title) else None,
                    1 if a.has_cover else 0,
                    a.fp_duration,
                    a.fp_compressed,
                    pack_raw(a.fp_raw),
                    now_iso(),
                    file_id,
                ),
            )

    def save_error(self, file_id: str, message: str) -> None:
        """Record a failed analysis; the file is retried only once it changes."""
        with self._lock:
            self._conn.execute(
                "UPDATE library_files SET error=?, analyzed_at=? WHERE id=?",
                (message, now_iso(), file_id),
            )

    def pending_lookup(self) -> list[tuple[str, int, str]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, fp_duration, fp_compressed FROM library_files "
                "WHERE looked_up_at IS NULL AND fp_compressed IS NOT NULL "
                "AND missing_since IS NULL ORDER BY path"
            ).fetchall()
        return [(r["id"], int(r["fp_duration"] or 0), str(r["fp_compressed"])) for r in rows]

    def save_lookup(self, file_id: str, match: AcoustidMatch | None) -> None:
        m = match
        with self._lock:
            self._conn.execute(
                "UPDATE library_files SET acoustid_id=?, acoustid_score=?, mbid=?, aid_artist=?, "
                "aid_title=?, looked_up_at=? WHERE id=?",
                (
                    m.track_id if m else None,
                    m.score if m else None,
                    m.mbid if m else None,
                    m.artist if m else None,
                    m.title if m else None,
                    now_iso(),
                    file_id,
                ),
            )

    def stats(self) -> dict[str, int]:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) FILTER (WHERE missing_since IS NULL) AS files, "
                "COUNT(*) FILTER (WHERE missing_since IS NOT NULL) AS missing, "
                "COUNT(*) FILTER (WHERE analyzed_at IS NOT NULL AND error IS NULL "
                "AND missing_since IS NULL) AS analysed, "
                "COUNT(*) FILTER (WHERE error IS NOT NULL AND missing_since IS NULL) AS errors, "
                "COUNT(*) FILTER (WHERE acoustid_id IS NOT NULL AND missing_since IS NULL) "
                "AS identified, "
                "COUNT(*) FILTER (WHERE lossless = 1 AND missing_since IS NULL) AS lossless, "
                "COALESCE(SUM(size) FILTER (WHERE missing_since IS NULL), 0) AS bytes "
                "FROM library_files"
            ).fetchone()
        return {k: int(v or 0) for k, v in dict(row).items()}

    def get(self, file_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM library_files WHERE id=?", (file_id,)
            ).fetchone()
        return dict(row) if row else None
