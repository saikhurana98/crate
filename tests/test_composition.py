"""Composition config helpers: enable flags + per-tier mux strategy selection."""

import os
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from crate.application.multiplex import MuxStrategy
from crate.composition import sources
from crate.composition.sources import (
    _enabled,
    _retention_days,
    _strategy,
    build_candidate_sources,
    build_candidate_streamers,
    build_maintenance,
    prune_attempts,
)
from crate.domain.candidate import Candidate
from crate.domain.download import DownloadOutcome
from crate.infrastructure.sqlite.db import connect
from crate.infrastructure.sqlite.store import RequestStore


def test_enabled_accepts_truthy_values(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    for val in ("1", "true", "YES"):
        monkeypatch.setenv("CRATE_X", val)
        assert _enabled("CRATE_X") is True
    monkeypatch.setenv("CRATE_X", "0")
    assert _enabled("CRATE_X") is False
    monkeypatch.delenv("CRATE_X", raising=False)
    assert _enabled("CRATE_X") is False


def test_strategy_defaults_to_all(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.delenv("CRATE_MUX_TIER1", raising=False)
    assert _strategy(1) is MuxStrategy.ALL


def test_strategy_uniform_from_uniform_or_distribute(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("CRATE_MUX_TIER2", "uniform")
    assert _strategy(2) is MuxStrategy.UNIFORM
    monkeypatch.setenv("CRATE_MUX_TIER2", "distribute")
    assert _strategy(2) is MuxStrategy.UNIFORM
    monkeypatch.setenv("CRATE_MUX_TIER2", "all")
    assert _strategy(2) is MuxStrategy.ALL


def test_manual_monochrome_search_is_available_when_cascade_disabled(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    candidate = Candidate("monochrome", 1, "7", "Song", "Artist", None, "FLAC", True, 1, 2)

    def search(query: str, want: Any) -> list[Candidate]:
        return [candidate]

    def download(cand: Candidate, out_dir: Path, want: Any) -> DownloadOutcome:
        raise AssertionError("download should not run")

    monkeypatch.setattr(sources, "_enabled_source_names", lambda: [])
    monkeypatch.setitem(sources._CANDIDATE_FACTORIES, "monochrome", lambda: (search, download))

    available = build_candidate_sources()

    assert list(available) == ["monochrome"]
    assert available["monochrome"][0]("Song", None) == [candidate]


def test_candidate_sources_load_stored_credentials_in_worker_process(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    class Config:
        def secret_keys(self, name: str) -> set[str]:
            return {"CRATE_ANTRA_USERNAME", "CRATE_ANTRA_PASSWORD"} if name == "antra" else set()

        def get_secret(self, name: str, key: str) -> str:
            return {"CRATE_ANTRA_USERNAME": "user", "CRATE_ANTRA_PASSWORD": "pass"}[key]

    def factory() -> sources.CandidateSource:
        assert os.environ["CRATE_ANTRA_USERNAME"] == "user"
        assert os.environ["CRATE_ANTRA_PASSWORD"] == "pass"

        def search(query: str, want: Any) -> list[Candidate]:
            return []

        def download(cand: Candidate, out_dir: Path, want: Any) -> DownloadOutcome:
            raise AssertionError("download should not run")

        return search, download

    monkeypatch.delenv("CRATE_ANTRA_USERNAME", raising=False)
    monkeypatch.delenv("CRATE_ANTRA_PASSWORD", raising=False)
    config = Config()
    monkeypatch.setattr(sources, "_config_store_if_enabled", lambda: config)
    monkeypatch.setattr(sources, "_enabled_source_names", lambda: ["antra"])
    monkeypatch.setitem(sources._CANDIDATE_FACTORIES, "antra", factory)

    assert "antra" in build_candidate_sources()


def test_monochrome_audition_stream_is_available_when_cascade_disabled(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(sources, "_enabled_source_names", lambda: [])
    monkeypatch.setenv("CRATE_MONOCHROME_BASE", "https://mono.example/")

    streamers = build_candidate_streamers()

    assert streamers["monochrome"]("42") == "https://mono.example/track/42"


def _seed_old_attempt(db: Path) -> str:
    """A completed request with one 200-day-old attempt row; returns its id."""
    store = RequestStore(connect(db), requested_via="serve")
    rid = store.open_request("t")
    old = (datetime.now(UTC) - timedelta(days=200)).isoformat()
    connect(db).execute(
        "INSERT INTO attempts(request_id, source, tier, ok, detail, created_at) "
        "VALUES (?, 'ytdlp', 3, 0, 'ancient', ?)",
        (rid, old),
    )
    return rid


def test_retention_days_defaults_and_overrides(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.delenv("CRATE_ATTEMPTS_RETENTION_DAYS", raising=False)
    assert _retention_days() == 90
    monkeypatch.setenv("CRATE_ATTEMPTS_RETENTION_DAYS", "30")
    assert _retention_days() == 30


def test_prune_attempts_deletes_old_rows(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    db = tmp_path / "crate.db"
    rid = _seed_old_attempt(db)
    monkeypatch.setenv("CRATE_DB", str(db))
    monkeypatch.delenv("CRATE_ATTEMPTS_RETENTION_DAYS", raising=False)

    assert prune_attempts() == 1  # the 200-day-old row
    got = RequestStore(connect(db)).get_request(rid)
    assert got is not None and got["attempts"] == []


def test_build_maintenance_loop_prunes_then_stops(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    db = tmp_path / "crate.db"
    rid = _seed_old_attempt(db)
    monkeypatch.setenv("CRATE_DB", str(db))
    monkeypatch.setenv("CRATE_MAINTENANCE_INTERVAL", "0.01")  # prune almost immediately

    loop = build_maintenance()
    stop = threading.Event()
    t = threading.Thread(target=loop, args=(stop,))
    t.start()
    pruned = False
    try:
        # poll (bounded) until the recurring pass has run once and removed the row
        for _ in range(100):
            got = RequestStore(connect(db)).get_request(rid)
            if got is not None and got["attempts"] == []:
                pruned = True
                break
            threading.Event().wait(0.02)
    finally:
        stop.set()
        t.join(timeout=2.0)
    assert pruned  # the loop pruned the old attempt
    assert not t.is_alive()  # and honoured stop
