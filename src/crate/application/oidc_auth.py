"""SSO sign-in with Spotify as a linked account (ADR-0023).

With ``CRATE_OIDC_*`` set, the identity provider decides who someone is:
- **Sign-in** is OIDC authorization code + PKCE, with state and nonce. The ID
  token's claims are checked, userinfo is merged in, and the ``(issuer, sub)``
  user is upserted. Admin comes from the provider's groups when
  ``CRATE_OIDC_ADMIN_GROUPS`` is set, otherwise from the linked Spotify id
  (``CRATE_ADMIN_SPOTIFY_IDS``).
- **Spotify** is linked afterwards, only to read what crate needs (playlists,
  Liked Songs, plays). It goes through the existing Spotify callback, with a
  state bound to the signed-in user. Linking a Spotify account that belongs to an
  older Spotify-login user (from before SSO) folds this account into that one, so
  its history, tiers and downloads carry over. A Spotify account already linked to
  a different SSO user is refused.
- Sessions of users with no SSO identity (old Spotify logins) no longer count;
  they sign in again with SSO.
"""

from __future__ import annotations

import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from crate.application.auth import SessionStore, SpotifyProfile, SsoUserRepository, User
from crate.domain.oidc import (
    OidcError,
    OidcProfile,
    check_id_token,
    is_admin,
    jwt_claims,
    pkce_challenge,
    profile_from_claims,
)

PENDING_TTL_S = 600  # how long a sign-in may take between redirect and callback
_MAX_PENDING = 1024  # bounds memory if someone hammers /api/auth/login


class OidcPort(Protocol):
    issuer: str
    client_id: str

    def authorize_url(self, state: str, nonce: str, code_challenge: str) -> str: ...
    def exchange(self, code: str, code_verifier: str) -> dict[str, Any]: ...
    def userinfo(self, access_token: str) -> dict[str, Any] | None: ...


class SpotifyOAuth(Protocol):
    def authorize_url(self, redirect_uri: str, state: str, scope: str) -> str: ...
    def exchange_code_tokens(self, code: str, redirect_uri: str) -> dict[str, str]: ...
    def me(self, access_token: str) -> dict[str, Any]: ...


class SecretVault(Protocol):
    def put_secret(self, name: str, env_key: str, value: str) -> None: ...
    def delete_secrets(self, name: str) -> int: ...


class SpotifyTakenError(Exception):
    """That Spotify account is already linked to a different SSO user."""


class LinkUnavailableError(Exception):
    """Spotify linking isn't configured (no Spotify client or redirect URI)."""


@dataclass
class _Pending:
    nonce: str
    verifier: str
    expires: float


class OidcAuthProvider:
    mode = "oidc"

    def __init__(
        self,
        oidc: OidcPort,
        users: SsoUserRepository,
        sessions: SessionStore,
        vault: SecretVault,
        *,
        label: str = "SSO",
        spotify: SpotifyOAuth | None = None,
        spotify_redirect: str | None = None,
        spotify_scopes: str = "",
        admin_groups: frozenset[str] = frozenset(),
        groups_claim: str = "groups",
        admin_spotify_ids: frozenset[str] = frozenset(),
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._oidc = oidc
        self._users = users
        self._sessions = sessions
        self._vault = vault
        self.label = label
        self._spotify = spotify
        self._spotify_redirect = spotify_redirect
        self._spotify_scopes = spotify_scopes
        self._admin_groups = admin_groups
        self._groups_claim = groups_claim
        self._admin_spotify_ids = admin_spotify_ids
        self._clock = clock
        self._pending: dict[str, _Pending] = {}

    # --- sign-in ---
    def login_url(self, state: str) -> str:
        now = self._clock()
        for key in [k for k, p in self._pending.items() if p.expires < now]:
            del self._pending[key]
        if len(self._pending) >= _MAX_PENDING:
            raise OidcError("too many sign-ins in progress; try again shortly")
        nonce, verifier = secrets.token_urlsafe(24), secrets.token_urlsafe(48)
        self._pending[state] = _Pending(nonce, verifier, now + PENDING_TTL_S)
        return self._oidc.authorize_url(state, nonce, pkce_challenge(verifier))

    def complete_login(self, code: str, state: str) -> tuple[User, str]:
        now = self._clock()
        pending = self._pending.pop(state, None)
        if pending is None or pending.expires < now:
            raise OidcError("that sign-in expired or was already used; try again")
        tokens = self._oidc.exchange(code, pending.verifier)
        claims = jwt_claims(str(tokens["id_token"]))
        check_id_token(
            claims,
            issuer=self._oidc.issuer,
            client_id=self._oidc.client_id,
            nonce=pending.nonce,
            now=now,
        )
        access = tokens.get("access_token")
        info = self._oidc.userinfo(str(access)) if access else None
        if info:
            if str(info.get("sub")) != str(claims["sub"]):
                raise OidcError("the provider's userinfo is for a different user")
            claims = {**claims, **info, "sub": claims["sub"], "iss": claims["iss"]}
        profile = profile_from_claims(
            claims, issuer=self._oidc.issuer, groups_claim=self._groups_claim
        )
        user = self._with_admin(self._users.upsert_oidc_user(profile), profile)
        return user, self._sessions.create(user.id)

    def _with_admin(self, user: User, profile: OidcProfile | None = None) -> User:
        """Apply the admin rule: SSO groups when configured, else the linked Spotify
        id. The configured rule always wins, granting or revoking."""
        if self._admin_groups:
            if profile is None:
                return user  # groups are only known at sign-in
            want = is_admin(profile, self._admin_groups)
        elif self._admin_spotify_ids:
            want = user.spotify_id in self._admin_spotify_ids
        else:
            return user
        if user.is_admin != want:
            return self._users.set_admin(user.id, want) or user
        return user

    def resolve_session(self, raw_token: str) -> User | None:
        """The session's user, if they signed in with SSO (old Spotify-login
        sessions don't count once SSO is on)."""
        user_id = self._sessions.resolve(raw_token)
        user = self._users.get(user_id) if user_id else None
        return user if user is not None and user.oidc_sub else None

    def logout(self, raw_token: str) -> None:
        self._sessions.revoke(raw_token)

    # --- Spotify as a linked account ---
    @property
    def spotify_linkable(self) -> bool:
        return self._spotify is not None and bool(self._spotify_redirect)

    def _spotify_or_raise(self) -> tuple[SpotifyOAuth, str]:
        if self._spotify is None or not self._spotify_redirect:
            raise LinkUnavailableError("Spotify linking isn't configured")
        return self._spotify, self._spotify_redirect

    def spotify_link_url(self, state: str) -> str:
        spotify, redirect = self._spotify_or_raise()
        return spotify.authorize_url(redirect, state, self._spotify_scopes)

    def complete_spotify_link(self, code: str, user_id: str) -> User:
        """Link the Spotify account behind ``code`` to ``user_id``; returns the
        user now holding the session (the older account, after a fold-in)."""
        spotify, redirect = self._spotify_or_raise()
        tokens = spotify.exchange_code_tokens(code, redirect)
        me = spotify.me(tokens["access_token"])
        sid = str(me.get("id") or "")
        if not sid:
            raise RuntimeError("Spotify profile has no id")
        target = user_id
        owner = self._users.by_spotify_id(sid)
        if owner is not None and owner.id != user_id:
            if owner.oidc_sub:
                raise SpotifyTakenError("that Spotify account is linked to another user")
            # An account from before SSO: it keeps its history and takes this identity.
            self._users.merge_into(user_id, owner.id)
            self._vault.delete_secrets(f"spotify:{user_id}")
            target = owner.id
        profile = SpotifyProfile(
            spotify_id=sid,
            display_name=str(me.get("display_name") or sid),
            email=me.get("email"),
            avatar_url=me.get("avatar"),
        )
        user = self._users.link_spotify(target, profile)
        self._vault.put_secret(f"spotify:{target}", "refresh_token", tokens["refresh_token"])
        if tokens.get("scope"):
            self._vault.put_secret(f"spotify:{target}", "scopes", tokens["scope"])
        return self._with_admin(user)

    def unlink_spotify(self, user_id: str) -> None:
        self._users.unlink_spotify(user_id)
        self._vault.delete_secrets(f"spotify:{user_id}")
        if not self._admin_groups and self._admin_spotify_ids:
            self._users.set_admin(user_id, False)
