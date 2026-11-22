"""HTTP client for Monochrome's public Tracks API."""

from __future__ import annotations

import time
from collections.abc import Callable
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

import httpx

DEFAULT_BASE = "https://tracks.monochrome.st"
_CHUNK = 64 * 1024
_FETCH_ATTEMPTS = 4
_RATE_LIMIT_ATTEMPTS = 3
_MAX_RETRY_AFTER_S = 30.0


def _retry_after_s(response: httpx.Response, attempt: int, now: Callable[[], float]) -> float:
    """Return a bounded 429 delay, accepting seconds or an HTTP date."""
    raw = response.headers.get("Retry-After", "").strip()
    if raw:
        try:
            delay = float(raw)
        except ValueError:
            try:
                delay = parsedate_to_datetime(raw).timestamp() - now()
            except (TypeError, ValueError, OverflowError):
                delay = 2.0**attempt
    else:
        delay = 2.0**attempt
    return min(_MAX_RETRY_AFTER_S, max(0.0, delay))


class MonochromeClient:  # pragma: no cover - network I/O
    def __init__(
        self,
        base_url: str = DEFAULT_BASE,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], float] = time.time,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._sleep = sleep
        self._now = now
        # HTTPTransport retries transient connection failures, including temporary
        # DNS resolution errors. Monochrome sits behind Cloudflare and both
        # production hosts have occasionally observed EAI_AGAIN/EAI_NONAME from
        # their local resolvers; a single miss should not discard the source.
        resilient_transport = transport or httpx.HTTPTransport(retries=2)
        self._http = httpx.Client(
            timeout=20.0, follow_redirects=True, transport=resilient_transport
        )

    def search(self, query: str) -> dict[str, Any]:
        for attempt in range(_RATE_LIMIT_ATTEMPTS):
            resp = self._http.get(f"{self._base}/search/tracks", params={"q": query, "limit": 20})
            if resp.status_code != httpx.codes.TOO_MANY_REQUESTS:
                resp.raise_for_status()
                return dict(resp.json())
            if attempt + 1 < _RATE_LIMIT_ATTEMPTS:
                self._sleep(_retry_after_s(resp, attempt, self._now))
        raise RuntimeError("Monochrome search remained rate limited after bounded retries")

    def fetch(self, track_id: str, dest: Path) -> None:
        """Stream one FLAC, resuming when Monochrome closes a large response early."""
        if not track_id.isdecimal():
            raise ValueError("invalid Monochrome track ID")
        url = f"{self._base}/track/{track_id}"
        last_error: Exception | None = None
        transport_attempt = 0
        rate_limit_attempt = 0
        while transport_attempt < _FETCH_ATTEMPTS and rate_limit_attempt < _RATE_LIMIT_ATTEMPTS:
            offset = dest.stat().st_size if dest.exists() else 0
            headers = {"Range": f"bytes={offset}-"} if offset else {}
            try:
                with self._http.stream("GET", url, headers=headers, timeout=120.0) as resp:
                    if resp.status_code == httpx.codes.TOO_MANY_REQUESTS:
                        rate_limit_attempt += 1
                        if rate_limit_attempt >= _RATE_LIMIT_ATTEMPTS:
                            break
                        self._sleep(_retry_after_s(resp, rate_limit_attempt - 1, self._now))
                        continue
                    resp.raise_for_status()
                    append = offset > 0 and resp.status_code == httpx.codes.PARTIAL_CONTENT
                    if append:
                        content_range = resp.headers.get("content-range", "")
                        if not content_range.startswith(f"bytes {offset}-"):
                            raise RuntimeError("Monochrome returned an invalid resume range")
                    mode = "ab" if append else "wb"
                    # Unbuffered writes make the last received byte immediately
                    # visible to the next Range request after a transport error.
                    with dest.open(mode, buffering=0) as fh:
                        for chunk in resp.iter_bytes(_CHUNK):
                            fh.write(chunk)
                return
            except httpx.TransportError as exc:
                last_error = exc
                transport_attempt += 1
        if rate_limit_attempt >= _RATE_LIMIT_ATTEMPTS:
            raise RuntimeError("Monochrome download remained rate limited after bounded retries")
        raise RuntimeError(f"Monochrome download remained incomplete after retries: {last_error}")

    def close(self) -> None:
        self._http.close()
