"""Runtime cascade config persistence (Phase 2).

Stores the tier layout (JSON + a version counter for hot-reload), per-source
settings, and per-source credentials encrypted via :class:`SecretBox`. Pure
persistence — validation and defaults live in ``composition.catalog`` where the
source catalog is known. Shared across the console (writer) and serve (reader)
processes on the same WAL DB.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from typing import Any

from lbmd.infrastructure.crypto import SecretBox
from lbmd.infrastructure.sqlite.db import now_iso


class ConfigStore:
    def __init__(self, conn: sqlite3.Connection, box: SecretBox) -> None:
        self._conn = conn
        self._box = box
        self._lock = threading.Lock()

    # --- layout ---
    def get_layout(self) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT layout FROM app_config WHERE id=1").fetchone()
        return json.loads(row["layout"]) if row else None

    def seed_layout(self, layout: dict[str, Any]) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO app_config(id, layout, version, updated_at) "
                "VALUES (1, ?, 1, ?)",
                (json.dumps(layout), now_iso()),
            )

    def put_layout(self, layout: dict[str, Any]) -> int:
        with self._lock:
            self._conn.execute(
                "INSERT INTO app_config(id, layout, version, updated_at) VALUES (1, ?, 1, ?) "
                "ON CONFLICT(id) DO UPDATE SET layout=excluded.layout, "
                "version=app_config.version+1, updated_at=excluded.updated_at",
                (json.dumps(layout), now_iso()),
            )
            row = self._conn.execute("SELECT version FROM app_config WHERE id=1").fetchone()
            return int(row[0])

    def version(self) -> int:
        row = self._conn.execute("SELECT version FROM app_config WHERE id=1").fetchone()
        return int(row[0]) if row else 0

    # --- per-source settings ---
    def get_settings(self, name: str) -> dict[str, Any]:
        row = self._conn.execute(
            "SELECT settings FROM source_config WHERE name=?", (name,)
        ).fetchone()
        return json.loads(row["settings"]) if row else {}

    def put_settings(self, name: str, settings: dict[str, Any]) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO source_config(name, settings, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(name) DO UPDATE SET settings=excluded.settings, "
                "updated_at=excluded.updated_at",
                (name, json.dumps(settings), now_iso()),
            )

    # --- per-source credentials (encrypted) ---
    def put_secret(self, name: str, env_key: str, value: str) -> None:
        blob = self._box.encrypt(value)
        with self._lock:
            self._conn.execute(
                "INSERT INTO source_secret(name, env_key, ciphertext, updated_at) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(name, env_key) DO UPDATE SET "
                "ciphertext=excluded.ciphertext, updated_at=excluded.updated_at",
                (name, env_key, blob, now_iso()),
            )

    def secret_keys(self, name: str) -> set[str]:
        return {
            row["env_key"]
            for row in self._conn.execute("SELECT env_key FROM source_secret WHERE name=?", (name,))
        }

    def get_secret(self, name: str, env_key: str) -> str | None:
        row = self._conn.execute(
            "SELECT ciphertext FROM source_secret WHERE name=? AND env_key=?", (name, env_key)
        ).fetchone()
        return self._box.decrypt(row["ciphertext"]) if row else None
