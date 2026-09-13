"""AuthProvider (Login-with-Spotify orchestration): exchange -> /v1/me -> upsert
-> per-user refresh token in the vault -> session. Driven with a fake Spotify
client and real SQLite users/sessions + config store."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from crate.composition.sources import AuthProvider
from crate.infrastructure.crypto import SecretBox
from crate.infrastructure.sqlite.config_store import ConfigStore
from crate.infrastructure.sqlite.db import connect
from crate.infrastructure.sqlite.user_store import SqliteSessionStore, SqliteUserRepository


class FakeClient:
    def __init__(self, me: dict[str, Any]) -> None:
        self._me = me
        self.exchanged: str | None = None
        self.scope: str | None = None

    def authorize_url(self, redirect: str, state: str, scope: str) -> str:
        return f"https://auth?scope={scope}&state={state}&redirect_uri={redirect}"

    def exchange_code_tokens(self, code: str, redirect: str) -> dict[str, str]:
        self.exchanged = code
        out = {"access_token": "acc", "refresh_token": "ref-tok"}
        if self.scope is not None:
            out["scope"] = self.scope
        return out

    def me(self, access_token: str) -> dict[str, Any]:
        assert access_token == "acc"
        return self._me


def _provider(
    tmp_path: Path, me: dict[str, Any], admins: frozenset[str] = frozenset()
) -> tuple[AuthProvider, ConfigStore, FakeClient]:
    conn = connect(tmp_path / "c.db")
    config = ConfigStore(connect(tmp_path / "cfg.db"), SecretBox(bytes(range(32))))
    client = FakeClient(me)
    provider = AuthProvider(
        client,  # type: ignore[arg-type]
        "https://c/api/auth/callback",
        config,
        SqliteUserRepository(conn),
        SqliteSessionStore(conn),
        admin_spotify_ids=admins,
    )
    return provider, config, client


def test_login_url_uses_login_scopes(tmp_path: Path) -> None:
    provider, _, _ = _provider(tmp_path, {"id": "x"})
    url = provider.login_url("st8")
    assert "user-read-email" in url and "state=st8" in url


def test_complete_login_upserts_user_and_persists_per_user_token(tmp_path: Path) -> None:
    me = {"id": "spot9", "display_name": "Sai", "email": "s@x.io", "avatar": "http://a"}
    provider, config, client = _provider(tmp_path, me)

    user, raw = provider.complete_login("thecode")
    assert client.exchanged == "thecode"
    assert user.spotify_id == "spot9" and user.display_name == "Sai"
    # refresh token stored per-user in the vault (Phase B reads it back)
    assert config.get_secret(f"spotify:{user.id}", "refresh_token") == "ref-tok"
    # a live session resolves back to the same user
    assert provider.resolve_session(raw) == user


def test_resolve_session_none_and_logout(tmp_path: Path) -> None:
    me = {"id": "spot9", "display_name": "Sai", "email": None, "avatar": None}
    provider, _, _ = _provider(tmp_path, me)
    assert provider.resolve_session("bogus") is None
    _user, raw = provider.complete_login("c")
    provider.logout(raw)
    assert provider.resolve_session(raw) is None


def test_complete_login_rejects_profile_without_id(tmp_path: Path) -> None:
    provider, _, _ = _provider(tmp_path, {"id": "", "display_name": "x"})
    try:
        provider.complete_login("c")
        raise AssertionError("expected RuntimeError")
    except RuntimeError:
        pass


def test_login_url_requests_top_read_for_library_sync(tmp_path: Path) -> None:
    provider, _, _ = _provider(tmp_path, {"id": "x"})
    assert "user-top-read" in provider.login_url("s")


def test_complete_login_persists_granted_scopes(tmp_path: Path) -> None:
    me = {"id": "spot9", "display_name": "Sai", "email": None, "avatar": None}
    provider, config, client = _provider(tmp_path, me)
    client.scope = "user-read-email user-top-read"
    user, _ = provider.complete_login("c")
    assert config.get_secret(f"spotify:{user.id}", "scopes") == "user-read-email user-top-read"


def test_admin_bootstrap_grants_listed_spotify_id(tmp_path: Path) -> None:
    me = {"id": "spot9", "display_name": "Sai", "email": None, "avatar": None}
    provider, _, _ = _provider(tmp_path, me, admins=frozenset({"spot9"}))
    user, _ = provider.complete_login("c")
    assert user.is_admin is True


def test_admin_env_is_source_of_truth_revokes_when_unlisted(tmp_path: Path) -> None:
    me = {"id": "spot9", "display_name": "Sai", "email": None, "avatar": None}
    granted, _, _ = _provider(tmp_path, me, admins=frozenset({"spot9"}))
    assert granted.complete_login("c")[0].is_admin is True
    # same DB, env now lists someone else -> revoked at next login
    revoked, _, _ = _provider(tmp_path, me, admins=frozenset({"other"}))
    assert revoked.complete_login("c")[0].is_admin is False


def test_admin_untouched_when_env_unset(tmp_path: Path) -> None:
    me = {"id": "spot9", "display_name": "Sai", "email": None, "avatar": None}
    first, _, _ = _provider(tmp_path, me, admins=frozenset({"spot9"}))
    first.complete_login("c")
    # no env list -> the stored flag is left alone (not silently revoked)
    plain, _, _ = _provider(tmp_path, me)
    assert plain.complete_login("c")[0].is_admin is True
