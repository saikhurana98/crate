"""Authentication ports + policy (Phase 2 multi-user, ADR-0018 v5).

Login is "Log in with Spotify": one OAuth round-trip both authenticates the user
and links their Spotify account. This module owns the *use-case boundary* — the
repository/session Protocols and the pure token policy (ttl, hashing). The SQLite
implementations live in ``infrastructure/sqlite/user_store.py`` and the OAuth
wiring in the composition root; nothing here does I/O.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Protocol

# A session lasts 30 days; the cookie carries the raw token, the DB only its hash.
SESSION_TTL_S = 30 * 24 * 3600

# The synthetic user that owns pre-multi-user rows and API-key (automation) traffic.
SERVICE_USER_ID = "service"


def hash_token(raw_token: str) -> str:
    """sha256 hex of a raw session token — what we store and look up by, so a DB
    read never yields a token usable as a cookie."""
    return hashlib.sha256(raw_token.encode()).hexdigest()


@dataclass(frozen=True)
class SpotifyProfile:
    """The subset of ``GET /v1/me`` we persist as identity."""

    spotify_id: str
    display_name: str
    email: str | None = None
    avatar_url: str | None = None


@dataclass(frozen=True)
class User:
    id: str
    spotify_id: str | None
    display_name: str
    email: str | None = None
    avatar_url: str | None = None
    is_admin: bool = False


class UserRepository(Protocol):
    """Persistence for app users keyed on their Spotify identity."""

    def upsert_spotify_user(self, profile: SpotifyProfile) -> User:
        """Insert (minting a new id) or update-in-place the user for this
        ``spotify_id``; return the stored row."""
        ...

    def get(self, user_id: str) -> User | None: ...

    def by_spotify_id(self, spotify_id: str) -> User | None: ...


class SessionStore(Protocol):
    """Opaque bearer sessions. ``create`` returns the RAW token (shown once, set
    as the cookie); every other method takes the raw token and hashes internally."""

    def create(self, user_id: str, ttl_s: int = SESSION_TTL_S) -> str:
        """Mint a session for ``user_id``; return the raw token for the cookie."""
        ...

    def resolve(self, raw_token: str) -> str | None:
        """The ``user_id`` for a live (unexpired) session, else ``None``."""
        ...

    def revoke(self, raw_token: str) -> None:
        """Drop a session (logout). Idempotent."""
        ...

    def sweep_expired(self) -> int:
        """Delete expired sessions; return how many were removed."""
        ...
