"""OpenID Connect policy (pure): PKCE, ID-token claim checks, and the profile and
admin rules. Network calls live in ``infrastructure/oidc``; the login and
Spotify-link orchestration in ``application/oidc_auth``."""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

CLOCK_SKEW_S = 120  # tolerated clock difference with the identity provider


class OidcError(Exception):
    """A sign-in step failed (bad state, token, claims, or provider reply)."""


@dataclass(frozen=True)
class OidcProfile:
    """Who signed in: the stable identity (``issuer`` + ``sub``) plus display fields."""

    issuer: str
    sub: str
    display_name: str
    email: str | None = None
    avatar_url: str | None = None
    groups: tuple[str, ...] = ()


def pkce_challenge(verifier: str) -> str:
    """The S256 code challenge for a PKCE ``verifier`` (RFC 7636)."""
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def jwt_claims(token: str) -> dict[str, Any]:
    """The payload of a compact JWT, **without** checking its signature.

    Only for an ID token that came straight from the token endpoint over TLS, in
    exchange for a code plus PKCE verifier. OIDC Core 3.1.3.7 (6) allows TLS to
    stand in for the signature there; :func:`check_id_token` still checks the
    claims."""
    parts = token.split(".")
    if len(parts) != 3:  # noqa: PLR2004 - header.payload.signature
        raise OidcError("malformed ID token")
    try:
        claims = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
    except ValueError as exc:
        raise OidcError("malformed ID token") from exc
    if not isinstance(claims, dict):
        raise OidcError("malformed ID token")
    return claims


def _same_issuer(a: object, b: str) -> bool:
    return str(a or "").rstrip("/") == b.rstrip("/")


def check_id_token(
    claims: Mapping[str, Any], *, issuer: str, client_id: str, nonce: str, now: float
) -> None:
    """Raise :class:`OidcError` unless the ID token is for us, from our issuer,
    unexpired, and carries this login's nonce (OIDC Core 3.1.3.7)."""
    if not _same_issuer(claims.get("iss"), issuer):
        raise OidcError("ID token is from a different issuer")
    aud = claims.get("aud")
    audiences = [aud] if isinstance(aud, str) else [str(a) for a in aud or []]
    if client_id not in audiences:
        raise OidcError("ID token is for a different client")
    if len(audiences) > 1 and claims.get("azp") not in (None, client_id):
        raise OidcError("ID token was issued to a different party")
    exp = claims.get("exp")
    if not isinstance(exp, int | float) or exp + CLOCK_SKEW_S < now:
        raise OidcError("ID token has expired")
    iat = claims.get("iat")
    if isinstance(iat, int | float) and iat - CLOCK_SKEW_S > now:
        raise OidcError("ID token is from the future")
    if not nonce or claims.get("nonce") != nonce:
        raise OidcError("ID token nonce doesn't match this sign-in")
    if not claims.get("sub"):
        raise OidcError("ID token has no subject")


def _groups(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return tuple(g for g in (s.strip() for s in value.replace(",", " ").split()) if g)
    if isinstance(value, list | tuple):
        return tuple(str(g) for g in value if str(g).strip())
    return ()


def profile_from_claims(
    claims: Mapping[str, Any], *, issuer: str, groups_claim: str = "groups"
) -> OidcProfile:
    """The profile to store, from ID-token claims merged with userinfo."""
    sub = str(claims["sub"])
    name = next(
        (
            str(claims[k]).strip()
            for k in ("name", "preferred_username", "nickname", "email")
            if claims.get(k) and str(claims[k]).strip()
        ),
        sub,
    )
    email = claims.get("email")
    picture = claims.get("picture")
    return OidcProfile(
        issuer=issuer.rstrip("/"),
        sub=sub,
        display_name=name,
        email=str(email) if email else None,
        avatar_url=str(picture) if picture else None,
        groups=_groups(claims.get(groups_claim)),
    )


def is_admin(profile: OidcProfile, admin_groups: frozenset[str]) -> bool:
    """Admin when the user is in any configured admin group."""
    return bool(admin_groups.intersection(profile.groups))
