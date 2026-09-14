"""SQLite storage for duplicate grouping: edges, ignores, groups and members, plus
the Dedup page's reads."""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterable
from typing import Any

from crate.application.library_dedup import GroupRecord, IndexRow
from crate.domain.chromaprint import unpack_raw
from crate.domain.library_dedup import Edge, pair
from crate.infrastructure.sqlite.db import now_iso

_CHUNK = 500
_FACT_COLS = (
    "id, path, size, lossless, bit_depth, sample_rate, bitrate, duration_s, artist, album, "
    "title, album_artist, track_no, disc_no, has_cover"
)
_MEMBER_COLS = (
    "f.id, f.path, f.size, f.codec, f.lossless, f.bitrate, f.bit_depth, f.sample_rate, "
    "f.duration_s, f.artist, f.album, f.title, f.has_cover, f.acoustid_id, f.acoustid_score, "
    "f.aid_artist, f.aid_title, f.mbid"
)


class SqliteDedupStore:
    def __init__(self, conn: sqlite3.Connection, *, lock: threading.Lock | None = None) -> None:
        self._conn = conn
        self._lock = lock or threading.Lock()

    # --- grouping (serve) ---
    def live_rows(self) -> list[IndexRow]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, path, size, COALESCE(duration_s, fp_duration) AS dur, acoustid_id, "
                "acoustid_score, content_hash, fp_raw, MAX(COALESCE(analyzed_at, ''), "
                "COALESCE(looked_up_at, '')) AS changed FROM library_files "
                "WHERE missing_since IS NULL"
            ).fetchall()
        return [
            IndexRow(
                id=r["id"],
                path=r["path"],
                size=int(r["size"]),
                duration_s=r["dur"],
                acoustid_id=r["acoustid_id"],
                acoustid_score=r["acoustid_score"],
                content_hash=r["content_hash"],
                changed_at=r["changed"] or None,
                raw=unpack_raw(r["fp_raw"]) if r["fp_raw"] else [],
            )
            for r in rows
        ]

    def last_grouped_at(self) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM dedup_state WHERE key='last_grouped_at'"
            ).fetchone()
        return str(row[0]) if row and row[0] else None

    def set_last_grouped_at(self, ts: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO dedup_state(key, value) VALUES ('last_grouped_at', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (ts,),
            )

    def set_hash(self, file_id: str, digest: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE library_files SET content_hash=? WHERE id=?", (digest, file_id)
            )

    def replace_edges(self, dirty: set[str], edges: list[Edge]) -> None:
        """Drop edges touching ``dirty`` files or files no longer in the index, then
        write ``edges``. Edges between unchanged files survive."""
        with self._lock, self._conn:
            live = {
                str(r[0])
                for r in self._conn.execute(
                    "SELECT id FROM library_files WHERE missing_since IS NULL"
                )
            }
            stale = [
                (a, b, k)
                for a, b, k in self._conn.execute("SELECT file_a, file_b, kind FROM dup_edges")
                if a in dirty or b in dirty or a not in live or b not in live
            ]
            self._conn.executemany(
                "DELETE FROM dup_edges WHERE file_a=? AND file_b=? AND kind=?", stale
            )
            self._conn.executemany(
                "INSERT OR REPLACE INTO dup_edges(file_a, file_b, kind, score) VALUES (?, ?, ?, ?)",
                [(e.a, e.b, e.kind, e.score) for e in edges],
            )

    def all_edges(self) -> list[Edge]:
        with self._lock:
            rows = self._conn.execute("SELECT file_a, file_b, kind, score FROM dup_edges")
            return [Edge(str(a), str(b), str(k), float(s)) for a, b, k, s in rows]

    def ignored_pairs(self) -> set[tuple[str, str]]:
        with self._lock:
            return {
                (str(a), str(b))
                for a, b in self._conn.execute("SELECT file_a, file_b FROM dup_ignores")
            }

    def facts(self, ids: Iterable[str]) -> dict[str, dict[str, Any]]:
        wanted = list(ids)
        out: dict[str, dict[str, Any]] = {}
        with self._lock:
            for start in range(0, len(wanted), _CHUNK):
                chunk = wanted[start : start + _CHUNK]
                marks = ",".join("?" * len(chunk))
                for r in self._conn.execute(
                    f"SELECT {_FACT_COLS} FROM library_files WHERE id IN ({marks})", chunk
                ):
                    out[str(r["id"])] = dict(r)
        return out

    def referenced_paths(self) -> set[str]:
        with self._lock:
            return {str(r[0]) for r in self._conn.execute("SELECT path FROM downloads")}

    def replace_groups(self, groups: list[GroupRecord]) -> None:
        """Rebuild the groups; a group that survives keeps its first-seen time."""
        now = now_iso()
        with self._lock, self._conn:
            first_seen = dict(self._conn.execute("SELECT id, created_at FROM dup_groups"))
            self._conn.execute("DELETE FROM dup_members")
            self._conn.execute("DELETE FROM dup_groups")
            self._conn.executemany(
                "INSERT INTO dup_groups(id, reason, confidence, variant, keeper_file_id, "
                "reclaimable_bytes, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        g.id,
                        g.reason,
                        g.confidence,
                        1 if g.variant else 0,
                        g.keeper_file_id,
                        g.reclaimable_bytes,
                        first_seen.get(g.id, now),
                    )
                    for g in groups
                ],
            )
            self._conn.executemany(
                "INSERT INTO dup_members(group_id, file_id, rank, score_vs_keeper, reasons_json, "
                "facts_json) VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (
                        g.id,
                        m.file_id,
                        m.rank,
                        m.score_vs_keeper,
                        json.dumps(m.reasons),
                        json.dumps(m.facts),
                    )
                    for g in groups
                    for m in g.members
                ],
            )

    # --- the Dedup page (console) ---
    def overview(self) -> dict[str, Any]:
        with self._lock:
            idx = self._conn.execute(
                "SELECT COUNT(*) FILTER (WHERE missing_since IS NULL) AS files, "
                "COUNT(*) FILTER (WHERE missing_since IS NULL AND analyzed_at IS NOT NULL "
                "AND error IS NULL) AS analysed, "
                "COUNT(*) FILTER (WHERE missing_since IS NULL AND acoustid_id IS NOT NULL) "
                "AS identified, "
                "COUNT(*) FILTER (WHERE missing_since IS NULL AND error IS NOT NULL) AS errors, "
                "COALESCE(SUM(size) FILTER (WHERE missing_since IS NULL), 0) AS bytes "
                "FROM library_files"
            ).fetchone()
            grp = self._conn.execute(
                "SELECT COUNT(*) AS groups, COALESCE(SUM(variant), 0) AS variants, "
                "COALESCE(SUM(reclaimable_bytes), 0) AS reclaimable FROM dup_groups"
            ).fetchone()
            extra = self._conn.execute(
                "SELECT COUNT(*) FROM dup_members m JOIN dup_groups g ON g.id = m.group_id "
                "WHERE m.rank > 0 AND g.variant = 0"
            ).fetchone()
            last = self._conn.execute(
                "SELECT value FROM dedup_state WHERE key='last_grouped_at'"
            ).fetchone()
        return {
            **{k: int(v or 0) for k, v in dict(idx).items()},
            "groups": int(grp["groups"]),
            "variant_groups": int(grp["variants"]),
            "duplicate_files": int(extra[0]),
            "reclaimable_bytes": int(grp["reclaimable"]),
            "last_grouped_at": last[0] if last else None,
        }

    def groups(
        self, *, limit: int = 50, offset: int = 0, variants: bool | None = None
    ) -> list[dict[str, Any]]:
        """Groups, biggest win first (variants last), each with ranked members."""
        where, params = "", []
        if variants is not None:
            where, params = "WHERE variant = ?", [1 if variants else 0]
        with self._lock:
            groups = [
                dict(r)
                for r in self._conn.execute(
                    f"SELECT * FROM dup_groups {where} "
                    "ORDER BY variant, reclaimable_bytes DESC, id LIMIT ? OFFSET ?",
                    [*params, limit, offset],
                )
            ]
            if not groups:
                return []
            marks = ",".join("?" * len(groups))
            rows = self._conn.execute(
                f"SELECT m.group_id, m.rank, m.score_vs_keeper, m.reasons_json, m.facts_json, "
                f"{_MEMBER_COLS} FROM dup_members m JOIN library_files f ON f.id = m.file_id "
                f"WHERE m.group_id IN ({marks}) ORDER BY m.group_id, m.rank",
                [g["id"] for g in groups],
            ).fetchall()
        by_group: dict[str, list[dict[str, Any]]] = {}
        for r in rows:
            member = dict(r)
            member["reasons"] = json.loads(member.pop("reasons_json"))
            member.update(json.loads(member.pop("facts_json")))
            member["lossless"] = bool(member["lossless"])
            member["has_cover"] = bool(member["has_cover"])
            member["is_keeper"] = member["rank"] == 0
            by_group.setdefault(str(member.pop("group_id")), []).append(member)
        for g in groups:
            g["variant"] = bool(g["variant"])
            g["members"] = by_group.get(g["id"], [])
        return groups

    def file_path(self, file_id: str) -> str | None:
        """A live file's path relative to the library root (for playback)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT path FROM library_files WHERE id=? AND missing_since IS NULL", (file_id,)
            ).fetchone()
        return str(row[0]) if row else None

    def ignore_group(self, group_id: str, user_id: str | None) -> int | None:
        """Mark every pair in the group "intentionally different" and drop the group.
        Returns how many pairs were recorded, or None for an unknown group."""
        now = now_iso()
        with self._lock, self._conn:
            ids = [
                str(r[0])
                for r in self._conn.execute(
                    "SELECT file_id FROM dup_members WHERE group_id=?", (group_id,)
                )
            ]
            if not ids:
                return None
            pairs = [pair(x, y) for i, x in enumerate(ids) for y in ids[i + 1 :]]
            self._conn.executemany(
                "INSERT OR IGNORE INTO dup_ignores(file_a, file_b, user_id, created_at) "
                "VALUES (?, ?, ?, ?)",
                [(a, b, user_id, now) for a, b in pairs],
            )
            self._conn.execute("DELETE FROM dup_members WHERE group_id=?", (group_id,))
            self._conn.execute("DELETE FROM dup_groups WHERE id=?", (group_id,))
        return len(pairs)
