"""AcoustID lookup client for the advisory review hint (ADR-0019).

Posts a compressed Chromaprint fingerprint + duration and returns the raw lookup
JSON; parsing into an :class:`~crate.domain.identity.IdentityHint` is the pure
``application.identify.parse_acoustid``. Free service, ~3 req/s; the key is the
per-app client key (``CRATE_ACOUSTID_KEY``), not a secret credential.

The ``meta`` values are **space-separated** on purpose: form-encoding turns the
spaces into ``+`` on the wire, which AcoustID reads as its list separator. Passing
a literal ``+`` gets percent-escaped to ``%2B`` and the server silently drops the
whole ``meta`` — the exact bug the spike hit (recordings/MBID came back empty).
The transport is injectable so that wire contract is locked by a test without the
network.
"""

from __future__ import annotations

from typing import Any

import httpx

_LOOKUP_URL = "https://api.acoustid.org/v2/lookup"
# No ISRC in meta on purpose (0% resolution on the real library); recordings +
# releases are what the hint needs.
_META = "recordings releases"


class AcoustidClient:
    def __init__(
        self,
        key: str,
        *,
        base_url: str = _LOOKUP_URL,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._key = key
        self._url = base_url
        self._http = httpx.Client(timeout=20.0, transport=transport)

    def lookup(self, duration: int, fingerprint: str) -> dict[str, Any]:
        """POST one fingerprint lookup, return the parsed JSON (raises on HTTP error)."""
        resp = self._http.post(
            self._url,
            data={
                "client": self._key,
                "duration": duration,
                "fingerprint": fingerprint,
                "meta": _META,
                "format": "json",
            },
        )
        resp.raise_for_status()
        return dict(resp.json())
