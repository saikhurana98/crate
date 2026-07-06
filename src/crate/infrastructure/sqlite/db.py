"""SQLite connection + a tiny hand-rolled migration runner (ADR-0004).

No ORM. WAL mode gives read-during-write concurrency so the console (reader
process) never blocks the shim (writer process) on the same DB file. The file
must live on a real local filesystem — NOT NFS — because SQLite locking is
unreliable over NFS (ADR-0018).
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

_MIGRATIONS = Path(__file__).parent / "migrations"

_PRAGMAS = (
    "PRAGMA journal_mode=WAL",
    "PRAGMA synchronous=NORMAL",
    "PRAGMA foreign_keys=ON",
    "PRAGMA busy_timeout=5000",
)


def now_iso() -> str:
    """UTC timestamp, ISO-8601, used for every ``*_at`` column."""
    return datetime.now(UTC).isoformat()


def connect(db_path: Path) -> sqlite3.Connection:
    """Open (creating parents), apply PRAGMAs, run pending migrations."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    for pragma in _PRAGMAS:
        conn.execute(pragma)
    migrate(conn)
    return conn


def migrate(conn: sqlite3.Connection) -> list[str]:
    """Apply every ``migrations/*.sql`` not yet recorded; return those applied."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations "
        "(version TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    applied = {row["version"] for row in conn.execute("SELECT version FROM schema_migrations")}
    ran: list[str] = []
    for path in sorted(_MIGRATIONS.glob("*.sql")):
        version = path.name
        if version in applied:
            continue
        conn.executescript(path.read_text())
        conn.execute(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
            (version, now_iso()),
        )
        ran.append(version)
    return ran
