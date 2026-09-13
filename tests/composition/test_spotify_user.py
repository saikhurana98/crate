"""SpotifyUser (Phase 2 consolidation): per-user player + library reads over each
user's stored refresh token, with per-user access-token caching. Fake client,
real config vault + request store."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from crate.composition.sources import SpotifyUser
from crate.infrastructure.crypto import SecretBox
from crate.infrastructure.spotify.client import LOGIN_SCOPES, RefreshedTokens
from crate.infrastructure.sqlite.config_store import ConfigStore
from crate.infrastructure.sqlite.db import connect
from crate.infrastructure.sqlite.store import RequestStore


class FakeClient:
    def __init__(self) -> None:
        self.refreshes = 0

    rotate_to: str | None = None  # a rotated refresh token Spotify hands back
    scope: str | None = None  # the granted scope string Spotify reports

    def refresh_tokens(self, refresh_token: str) -> RefreshedTokens:
        self.refreshes += 1
        return RefreshedTokens(f"acc-for-{refresh_token}", self.rotate_to, self.scope)

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

    def user_playlist_snapshot(self, token: str, playlist_id: str) -> str | None:
        return f"snap-{playlist_id}"

    def recently_played_after(
        self, token: str, after_ms: int | None
    ) -> tuple[list[dict[str, Any]], int | None]:
        return [{"id": "t1", "title": "Past", "played_at": "2026-09-01T10:00:00Z"}], 42

    def top_tracks(self, token: str, time_range: str, limit: int = 100) -> list[dict[str, Any]]:
        return [{"id": f"top-{time_range}", "title": "Top"}][:limit]


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
    assert su.playlist_snapshot("u1", "PID") == "snap-PID"


def test_not_connected_raises(tmp_path: Path) -> None:
    su, _, _, _, _ = _wire(tmp_path)
    try:
        su.player("ghost")
        raise AssertionError("expected RuntimeError")
    except RuntimeError:
        pass


def test_refresh_persists_rotated_token_and_granted_scope(tmp_path: Path) -> None:
    su, config, _, client, _ = _wire(tmp_path)
    config.put_secret("spotify:u1", "refresh_token", "rt")
    client.rotate_to = "rt2"
    client.scope = "user-top-read user-read-email"
    su.player("u1")
    # rotation persisted, or the old token eventually dies (and serve would be stranded)
    assert config.get_secret("spotify:u1", "refresh_token") == "rt2"
    assert config.get_secret("spotify:u1", "scopes") == "user-top-read user-read-email"


def test_refresh_without_rotation_keeps_token(tmp_path: Path) -> None:
    su, config, _, _, _ = _wire(tmp_path)
    config.put_secret("spotify:u1", "refresh_token", "rt")
    su.player("u1")
    assert config.get_secret("spotify:u1", "refresh_token") == "rt"
    assert config.get_secret("spotify:u1", "scopes") is None  # nothing reported


def test_missing_scopes_drives_relogin(tmp_path: Path) -> None:
    su, config, _, _, _ = _wire(tmp_path)
    assert su.missing_scopes("u1") == []  # not connected -> nothing to upgrade
    config.put_secret("spotify:u1", "refresh_token", "rt")
    assert su.missing_scopes("u1") == ["user-top-read"]  # legacy token, no scope recorded
    config.put_secret("spotify:u1", "scopes", LOGIN_SCOPES)
    assert su.missing_scopes("u1") == []


def test_ledger_and_tier_reads_delegate_with_the_user_token(tmp_path: Path) -> None:
    su, config, _, _, _ = _wire(tmp_path)
    config.put_secret("spotify:u1", "refresh_token", "rt")
    plays, cursor = su.recent_plays_after("u1", None)
    assert plays[0]["id"] == "t1" and cursor == 42
    assert su.top_tracks("u1", "long_term")[0]["id"] == "top-long_term"
