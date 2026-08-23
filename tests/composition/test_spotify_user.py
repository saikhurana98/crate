"""SpotifyUser (Phase 2 consolidation): per-user player + library reads over each
user's stored refresh token, with per-user access-token caching. Fake client,
real config vault + request store."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from crate.composition.sources import SpotifyUser
from crate.infrastructure.crypto import SecretBox
from crate.infrastructure.sqlite.config_store import ConfigStore
from crate.infrastructure.sqlite.db import connect
from crate.infrastructure.sqlite.store import RequestStore


class FakeClient:
    def __init__(self) -> None:
        self.refreshes = 0

    def refresh_access_token(self, refresh_token: str) -> str:
        self.refreshes += 1
        return f"acc-for-{refresh_token}"

    def currently_playing(self, token: str) -> dict[str, Any] | None:
        return {"title": "Now", "query": "Now q"}

    def recently_played(self, token: str, limit: int = 8) -> list[dict[str, Any]]:
        return [{"title": "Past", "query": "Past q"}]

    def current_user_playlists(self, token: str) -> list[dict[str, Any]]:
        return [{"id": "p1", "name": "Focus", "count": 1, "image": None}]

    def saved_tracks(self, token: str, max_tracks: int = 2000) -> list[dict[str, Any]]:
        return [{"query": "liked", "artist": "A", "title": "T"}]

    def user_playlist_tracks(
        self, token: str, playlist_id: str, max_tracks: int = 2000
    ) -> list[dict[str, Any]]:
        return [{"query": f"pl {playlist_id}", "artist": "A", "title": "T"}]


def _wire(tmp_path: Path) -> tuple[SpotifyUser, ConfigStore, RequestStore, FakeClient, list[float]]:
    config = ConfigStore(connect(tmp_path / "cfg.db"), SecretBox(bytes(range(32))))
    store = RequestStore(connect(tmp_path / "crate.db"))
    clock = [0.0]
    client = FakeClient()
    su = SpotifyUser(client, config, store, now=lambda: clock[0])  # type: ignore[arg-type]
    return su, config, store, client, clock


def test_connected_reflects_stored_token(tmp_path: Path) -> None:
    su, config, _, _, _ = _wire(tmp_path)
    assert su.connected("u1") is False
    config.put_secret("spotify:u1", "refresh_token", "rt")
    assert su.connected("u1") is True


def test_player_annotates_in_library_and_caches_token(tmp_path: Path) -> None:
    su, config, _store, client, _clock = _wire(tmp_path)
    config.put_secret("spotify:u1", "refresh_token", "rt")

    p = su.player("u1")
    assert p["now_playing"]["title"] == "Now"
    assert p["now_playing"]["in_library"] is False
    assert p["recent"][0]["in_library"] is False
    assert client.refreshes == 1
    su.player("u1")  # within TTL -> token reused, no second refresh
    assert client.refreshes == 1


def test_library_methods_delegate(tmp_path: Path) -> None:
    su, config, _, _, _ = _wire(tmp_path)
    config.put_secret("spotify:u1", "refresh_token", "rt")
    assert su.my_playlists("u1")[0]["name"] == "Focus"
    assert su.liked_songs("u1")[0]["query"] == "liked"
    assert su.playlist_tracks("u1", "PID")[0]["query"] == "pl PID"


def test_not_connected_raises(tmp_path: Path) -> None:
    su, _, _, _, _ = _wire(tmp_path)
    try:
        su.player("ghost")
        raise AssertionError("expected RuntimeError")
    except RuntimeError:
        pass
