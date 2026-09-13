"""SQLite storage for affinity tiers: per-user thresholds, per-tier auto-sync
flags, membership with first-seen/left history, and top-tracks snapshots.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterable
from typing import Any

from crate.application.affinity import TierMember
from crate.domain.affinity import AffinityThresholds, AffinityTier
from crate.infrastructure.sqlite.db import now_iso


class SqliteAffinityStore:
    def __init__(self, conn: sqlite3.Connection, *, lock: threading.Lock | None = None) -> None:
        self._conn = conn
        self._lock = lock or threading.Lock()

    # --- thresholds (per user) ---
    def thresholds(self, user_id: str) -> AffinityThresholds:
        with self._lock:
            row = self._conn.execute(
                "SELECT thresholds_json FROM affinity_prefs WHERE user_id=?", (user_id,)
            ).fetchone()
        raw = json.loads(row["thresholds_json"]) if row else None
        return AffinityThresholds.from_dict(raw)  # validated + clamped on every read

    def set_thresholds(self, user_id: str, th: AffinityThresholds) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO affinity_prefs(user_id, thresholds_json, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET thresholds_json=excluded.thresholds_json, "
                "updated_at=excluded.updated_at",
                (user_id, json.dumps(th.to_dict()), now_iso()),
            )

    # --- auto-sync flags (per user, per tier) ---
    def tier_settings(self, user_id: str) -> dict[int, dict[str, Any]]:
        """Every tier's ``{auto_sync, enabled_at}`` (off by default)."""
        out: dict[int, dict[str, Any]] = {
            int(t): {"auto_sync": False, "enabled_at": None} for t in AffinityTier
        }
        with self._lock:
            rows = self._conn.execute(
                "SELECT tier, auto_sync, enabled_at FROM affinity_settings WHERE user_id=?",
                (user_id,),
            ).fetchall()
        for r in rows:
            out[int(r["tier"])] = {"auto_sync": bool(r["auto_sync"]), "enabled_at": r["enabled_at"]}
        return out

    def set_auto_sync(self, user_id: str, tier: AffinityTier, on: bool) -> None:
        """Switch a tier's auto-sync. ``enabled_at`` records when it last went on."""
        with self._lock:
            self._conn.execute(
                "INSERT INTO affinity_settings(user_id, tier, auto_sync, enabled_at) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(user_id, tier) DO UPDATE SET "
                "auto_sync=excluded.auto_sync, enabled_at=CASE WHEN excluded.auto_sync=1 "
                "THEN excluded.enabled_at ELSE affinity_settings.enabled_at END",
                (user_id, int(tier), 1 if on else 0, now_iso() if on else None),
            )

    # --- membership ---
    def apply_members(
        self,
        user_id: str,
        tier: AffinityTier,
        members: Iterable[TierMember],
        *,
        approximate: bool = False,
    ) -> tuple[list[str], list[str]]:
        """Replace the tier's active membership with ``members``, keeping history.

        New keys are stamped with ``first_seen_at``; keys that dropped out get
        ``left_at``; a key that comes back has its ``left_at`` cleared. Returns
        ``(entered, left)`` merge keys, sorted."""
        now = now_iso()
        rows = [
            (
                user_id,
                int(tier),
                m.merge_key,
                m.spotify_id,
                m.artist,
                m.title,
                m.score,
                1 if approximate else 0,
                now,
                now,
                json.dumps(m.stats),
            )
            for m in members
        ]
        current = {r[2] for r in rows}
        with self._lock:
            previous = {
                str(r[0])
                for r in self._conn.execute(
                    "SELECT merge_key FROM affinity_members WHERE user_id=? AND tier=? "
                    "AND left_at IS NULL",
                    (user_id, int(tier)),
                )
            }
            self._conn.executemany(
                "INSERT INTO affinity_members(user_id, tier, merge_key, spotify_id, artist, title, "
                "score, approximate, first_seen_at, last_seen_at, stats_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(user_id, tier, merge_key) DO UPDATE SET "
                "spotify_id=excluded.spotify_id, artist=excluded.artist, title=excluded.title, "
                "score=excluded.score, approximate=excluded.approximate, "
                "last_seen_at=excluded.last_seen_at, left_at=NULL, stats_json=excluded.stats_json",
                rows,
            )
            left = sorted(previous - current)
            self._conn.executemany(
                "UPDATE affinity_members SET left_at=? WHERE user_id=? AND tier=? AND merge_key=?",
                [(now, user_id, int(tier), key) for key in left],
            )
        return sorted(current - previous), left

    def members(
        self,
        user_id: str,
        tier: AffinityTier,
        *,
        include_left: bool = False,
        limit: int = 500,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """A tier's members, best-ranked first (the Sync page's tier list)."""
        sql = "SELECT * FROM affinity_members WHERE user_id=? AND tier=?"
        if not include_left:
            sql += " AND left_at IS NULL"
        sql += " ORDER BY score DESC, artist, title LIMIT ? OFFSET ?"
        with self._lock:
            rows = self._conn.execute(sql, (user_id, int(tier), limit, offset)).fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            d = dict(r)
            d["stats"] = json.loads(d.pop("stats_json") or "{}")
            d["approximate"] = bool(d["approximate"])
            out.append(d)
        return out

    def member_count(self, user_id: str, tier: AffinityTier) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM affinity_members WHERE user_id=? AND tier=? "
                "AND left_at IS NULL",
                (user_id, int(tier)),
            ).fetchone()
        return int(row[0]) if row else 0

    # --- top-tracks snapshots (pre-export approximation) ---
    def record_top_snapshot(self, user_id: str, time_range: str, ids: list[str]) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO top_snapshots(user_id, time_range, taken_at, ids_json) "
                "VALUES (?, ?, ?, ?)",
                (user_id, time_range, now_iso(), json.dumps(ids)),
            )
