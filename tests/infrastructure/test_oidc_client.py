"""OIDC relying-party client over a fake transport."""

from __future__ import annotations

import base64
from collections.abc import Mapping
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest

from crate.domain.oidc import OidcError
from crate.infrastructure.oidc.client import OidcClient

ISS = "https://auth.example.com/app"
DOC = {
    "issuer": ISS + "/",
    "authorization_endpoint": "https://auth.example.com/authorize?tenant=1",
    "token_endpoint": "https://auth.example.com/token",
    "userinfo_endpoint": "https://auth.example.com/userinfo",
}


class _Http:
    def __init__(self, doc: dict[str, Any], tokens: Any = None, info: Any = None) -> None:
        self.doc, self.tokens, self.info = doc, tokens, info
        self.gets: list[tuple[str, dict[str, str]]] = []
        self.posts: list[tuple[str, dict[str, str], dict[str, str]]] = []

    def get(self, url: str, headers: Mapping[str, str]) -> Any:
        self.gets.append((url, dict(headers)))
        return self.doc if url.endswith("openid-configuration") else self.info

    def post(self, url: str, form: dict[str, str], headers: dict[str, str]) -> Any:
        self.posts.append((url, form, headers))
        return self.tokens


def _client(
    http: _Http, secret: str | None = "s3:cret", scopes: str = "profile email"
) -> OidcClient:
    return OidcClient(
        ISS + "/",
        "crate",
        secret,
        "https://c/cb",
        get_json=http.get,
        post_form=http.post,
        scopes=scopes,
    )


def test_authorize_url_uses_discovery_pkce_and_openid_scope() -> None:
    http = _Http(DOC)
    c = _client(http)
    url = c.authorize_url("st", "nn", "chal")
    q = parse_qs(urlparse(url).query)
    assert url.startswith("https://auth.example.com/authorize?tenant=1&")
    assert q["scope"] == ["openid profile email"] and q["code_challenge_method"] == ["S256"]
    assert (q["state"], q["nonce"], q["code_challenge"], q["client_id"]) == (
        ["st"],
        ["nn"],
        ["chal"],
        ["crate"],
    )
    c.authorize_url("a", "b", "c")
    assert len(http.gets) == 1  # discovery cached


def test_discovery_must_match_and_be_complete() -> None:
    with pytest.raises(OidcError, match="different issuer"):
        _client(_Http(DOC | {"issuer": "https://evil"})).discovery()
    with pytest.raises(OidcError, match="incomplete"):
        _client(_Http({"issuer": ISS})).discovery()


def test_exchange_with_basic_auth_post_auth_and_public_client() -> None:
    http = _Http(DOC, tokens={"id_token": "x.y.z", "access_token": "at"})
    assert _client(http).exchange("code1", "ver1")["access_token"] == "at"
    url, form, headers = http.posts[0]
    assert (
        url == DOC["token_endpoint"]
        and form["code_verifier"] == "ver1"
        and "client_secret" not in form
    )
    assert base64.b64decode(headers["Authorization"].split()[1]).decode() == "crate:s3%3Acret"

    http = _Http(
        DOC | {"token_endpoint_auth_methods_supported": ["client_secret_post"]},
        tokens={"id_token": "t"},
    )
    _client(http).exchange("c", "v")
    assert (
        http.posts[0][1]["client_secret"] == "s3:cret" and "Authorization" not in http.posts[0][2]
    )

    http = _Http(DOC, tokens={"id_token": "t"})
    _client(http, secret=None).exchange("c", "v")
    assert http.posts[0][1]["client_id"] == "crate" and "Authorization" not in http.posts[0][2]

    with pytest.raises(OidcError, match="no ID token"):
        _client(_Http(DOC, tokens={"access_token": "a"})).exchange("c", "v")


def test_userinfo() -> None:
    http = _Http(DOC, info={"sub": "u1", "groups": ["g"]})
    assert _client(http).userinfo("at") == {"sub": "u1", "groups": ["g"]}
    assert http.gets[-1] == (DOC["userinfo_endpoint"], {"Authorization": "Bearer at"})
    assert _client(_Http(DOC, info="nope")).userinfo("at") is None
    no_ep = {k: v for k, v in DOC.items() if k != "userinfo_endpoint"}
    assert _client(_Http(no_ep)).userinfo("at") is None
