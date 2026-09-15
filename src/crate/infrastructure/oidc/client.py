"""A generic OpenID Connect relying party (authorization code + PKCE).

Works with any standards-compliant provider (Authentik, Authelia, Keycloak, Pocket
ID, Google, …) via discovery: ``{issuer}/.well-known/openid-configuration``.
Transport is injected, so this is unit-tested with fakes."""

from __future__ import annotations

import base64
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import quote, urlencode

from crate.domain.oidc import OidcError

GetJson = Callable[[str, Mapping[str, str]], Any]  # (url, headers) -> JSON
PostForm = Callable[[str, dict[str, str], dict[str, str]], Any]  # (url, form, headers) -> JSON

DEFAULT_SCOPES = "openid profile email"


class OidcClient:
    def __init__(
        self,
        issuer: str,
        client_id: str,
        client_secret: str | None,
        redirect_uri: str,
        *,
        get_json: GetJson,
        post_form: PostForm,
        scopes: str = DEFAULT_SCOPES,
    ) -> None:
        self.issuer = issuer.rstrip("/")
        self.client_id = client_id
        self._secret = client_secret or None
        self.redirect_uri = redirect_uri
        self._get = get_json
        self._post = post_form
        self.scopes = scopes if "openid" in scopes.split() else f"openid {scopes}"
        self._doc: dict[str, Any] | None = None

    def discovery(self) -> dict[str, Any]:
        """The provider's metadata, fetched once."""
        if self._doc is None:
            doc = self._get(f"{self.issuer}/.well-known/openid-configuration", {}) or {}
            if str(doc.get("issuer") or "").rstrip("/") != self.issuer:
                raise OidcError("the provider's discovery document names a different issuer")
            if not doc.get("authorization_endpoint") or not doc.get("token_endpoint"):
                raise OidcError("the provider's discovery document is incomplete")
            self._doc = dict(doc)
        return self._doc

    def authorize_url(self, state: str, nonce: str, code_challenge: str) -> str:
        endpoint = str(self.discovery()["authorization_endpoint"])
        query = urlencode(
            {
                "response_type": "code",
                "client_id": self.client_id,
                "redirect_uri": self.redirect_uri,
                "scope": self.scopes,
                "state": state,
                "nonce": nonce,
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
            }
        )
        return f"{endpoint}{'&' if '?' in endpoint else '?'}{query}"

    def exchange(self, code: str, code_verifier: str) -> dict[str, Any]:
        """Swap the callback code (+ PKCE verifier) for tokens, including the ID token."""
        doc = self.discovery()
        form = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.redirect_uri,
            "code_verifier": code_verifier,
        }
        headers = {"Accept": "application/json"}
        methods = doc.get("token_endpoint_auth_methods_supported") or ["client_secret_basic"]
        if self._secret is None:  # a public client: PKCE alone
            form["client_id"] = self.client_id
        elif "client_secret_basic" in methods:
            pair = f"{quote(self.client_id, safe='')}:{quote(self._secret, safe='')}"
            headers["Authorization"] = "Basic " + base64.b64encode(pair.encode()).decode()
        else:
            form |= {"client_id": self.client_id, "client_secret": self._secret}
        tokens = self._post(str(doc["token_endpoint"]), form, headers) or {}
        if not tokens.get("id_token"):
            raise OidcError("the provider returned no ID token (is the 'openid' scope allowed?)")
        return dict(tokens)

    def userinfo(self, access_token: str) -> dict[str, Any] | None:
        """Extra claims (e.g. groups) from the userinfo endpoint, if the provider has one."""
        endpoint = self.discovery().get("userinfo_endpoint")
        if not endpoint:
            return None
        data = self._get(str(endpoint), {"Authorization": f"Bearer {access_token}"})
        return dict(data) if isinstance(data, dict) else None
