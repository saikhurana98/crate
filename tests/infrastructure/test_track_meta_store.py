"""SqliteTrackMetaStore: the spotify_tracks cache used by previews."""

from __future__ import annotations

from pathlib import Path

from crate.infrastructure.sqlite.db import connect
from crate.infrastructure.sqlite.track_meta_store import SqliteTrackMetaStore


def test_round_trip_and_deezer_id_survives_a_meta_refresh(tmp_path: Path) -> None:
    store = SqliteTrackMetaStore(connect(tmp_path / "crate.db"))
    assert store.get("sid") is None
    store.put("sid", {"isrc": "USUM71703861", "title": "T", "artist": "A", "duration_s": 200})
    store.set_deezer_id("sid", "42")
    store.put("sid", {"isrc": "USUM71703861", "title": "T (Remastered)", "artist": "A"})
    row = store.get("sid")
    assert row is not None
    assert (row["title"], row["deezer_id"], row["duration_s"]) == ("T (Remastered)", "42", None)
    store.set_deezer_id("sid", None)
    assert store.get("sid")["deezer_id"] is None  # type: ignore[index]
