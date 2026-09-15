"""OIDC policy: PKCE, JWT payload decoding, ID-token checks, profile, admin."""

from __future__ import annotations

import base64
import json
from typing import Any

import pytest

from crate.domain.oidc import (
    OidcError,
    check_id_token,
    is_admin,
    jwt_claims,
    pkce_challenge,
    profile_from_claims,
)

ISS = "https://auth.example.com/app/"
NOW = 1_800_000_000.0


def jwt(claims: Any) -> str:
    def b64(obj: Any) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()

    return f"{b64({'alg': 'RS256'})}.{b64(claims)}.c2ln"


def good(**kw: Any) -> dict[str, Any]:
    return {
        "iss": ISS,
        "aud": "crate",
        "exp": NOW + 300,
        "iat": NOW,
        "nonce": "n1",
        "sub": "u1",
    } | kw


def test_pkce_challenge_rfc7636_vector() -> None:
    verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    assert pkce_challenge(verifier) == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"


def test_jwt_claims() -> None:
    assert jwt_claims(jwt({"sub": "x"})) == {"sub": "x"}
    for bad in ("a.b", "a.!!!.c", jwt(["not", "a", "dict"])):
        with pytest.raises(OidcError, match="malformed"):
            jwt_claims(bad)


def test_check_id_token_accepts_a_good_token() -> None:
    check_id_token(good(), issuer=ISS.rstrip("/"), client_id="crate", nonce="n1", now=NOW)
    check_id_token(
        good(aud=["crate", "x"], azp="crate"), issuer=ISS, client_id="crate", nonce="n1", now=NOW
    )
    check_id_token(good(exp=NOW - 60), issuer=ISS, client_id="crate", nonce="n1", now=NOW)  # skew


@pytest.mark.parametrize(
    ("claims", "nonce", "why"),
    [
        (good(iss="https://evil"), "n1", "issuer"),
        (good(aud="other"), "n1", "different client"),
        (good(aud=["crate", "x"], azp="x"), "n1", "different party"),
        (good(exp=NOW - 600), "n1", "expired"),
        (good(exp="soon"), "n1", "expired"),
        (good(iat=NOW + 600), "n1", "future"),
        (good(nonce="other"), "n1", "nonce"),
        (good(), "", "nonce"),
        (good(sub=""), "n1", "subject"),
    ],
)
def test_check_id_token_rejects(claims: dict[str, Any], nonce: str, why: str) -> None:
    with pytest.raises(OidcError, match=why):
        check_id_token(claims, issuer=ISS, client_id="crate", nonce=nonce, now=NOW)


def test_profile_and_admin() -> None:
    p = profile_from_claims(
        {
            "sub": "u1",
            "name": " Sai ",
            "email": "s@x.io",
            "picture": "http://p",
            "groups": ["crate-admins", "x"],
        },
        issuer=ISS,
    )
    assert (p.issuer, p.sub, p.display_name, p.email, p.avatar_url) == (
        ISS.rstrip("/"),
        "u1",
        "Sai",
        "s@x.io",
        "http://p",
    )
    assert is_admin(p, frozenset({"crate-admins"})) and not is_admin(p, frozenset({"ops"}))
    q = profile_from_claims(
        {"sub": "u2", "preferred_username": "sk", "roles": "a, b"}, issuer=ISS, groups_claim="roles"
    )
    assert (q.display_name, q.email, q.avatar_url, q.groups) == ("sk", None, None, ("a", "b"))
    assert profile_from_claims({"sub": "u3", "groups": 7}, issuer=ISS).display_name == "u3"
    assert profile_from_claims({"sub": "u3", "groups": 7}, issuer=ISS).groups == ()


def test_username_claim() -> None:
    p = profile_from_claims({"sub": "u1", "preferred_username": " saik "}, issuer=ISS)
    assert p.username == "saik"
    q = profile_from_claims({"sub": "u1", "nickname": "sk"}, issuer=ISS, username_claim="nickname")
    assert q.username == "sk"
    assert profile_from_claims({"sub": "u1"}, issuer=ISS).username is None
