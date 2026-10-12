"""Client for Rhythmax/ODSkyler's public TIDAL token and manifest services."""

from __future__ import annotations

import re
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from xml.etree import ElementTree

import httpx

DEFAULT_HIFI_BASE = "https://hifi.odskyler.com"
DEFAULT_AUTH_BASE = "https://auth.odskyler.workers.dev"
DEFAULT_CATALOG_BASE = "https://api.tidal.com/v1"
_CHUNK = 64 * 1024
_TOKEN_TTL_S = 45 * 60
_UNAUTHORIZED = 401
_NUMBER = re.compile(r"\$Number(?:%0(\d+)d)?\$")
_MANIFEST_QUALITIES = ("HI_RES_LOSSLESS", "HI_RES", "LOSSLESS")
Remux = Callable[[Path, Path], None]


def _default_remux(source: Path, dest: Path) -> None:  # pragma: no cover - subprocess I/O
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source),
            "-map",
            "0:a:0",
            "-c:a",
            "copy",
            "-f",
            "flac",
            str(dest),
        ],
        check=True,
        timeout=600,
    )


class TidalClient:  # pragma: no cover - network I/O; covered with MockTransport
    """Search TIDAL and turn its DASH lossless stream into a normal FLAC."""

    def __init__(
        self,
        hifi_base: str = DEFAULT_HIFI_BASE,
        auth_base: str = DEFAULT_AUTH_BASE,
        catalog_base: str = DEFAULT_CATALOG_BASE,
        *,
        transport: httpx.BaseTransport | None = None,
        remux: Remux = _default_remux,
    ) -> None:
        self._hifi = hifi_base.rstrip("/")
        self._auth = auth_base.rstrip("/")
        self._catalog = catalog_base.rstrip("/")
        resilient_transport = transport or httpx.HTTPTransport(retries=2)
        self._http = httpx.Client(
            timeout=30.0, follow_redirects=True, transport=resilient_transport
        )
        self._remux = remux
        self._tokens: dict[str, tuple[str, float]] = {}
        self._token_lock = threading.Lock()

    @staticmethod
    def _token_value(data: Any) -> tuple[str, int]:
        if isinstance(data, str):
            return data, _TOKEN_TTL_S
        if not isinstance(data, dict):
            raise ValueError("TIDAL token service returned an invalid response")
        token = data.get("token") or data.get("access_token") or data.get("accessToken")
        if not token:
            raise ValueError("TIDAL token service returned no token")
        ttl = int(data.get("expires_in") or data.get("expiresIn") or _TOKEN_TTL_S)
        return str(token), max(60, ttl - 30)

    def _token(self, kind: str, *, refresh: bool = False) -> str:
        with self._token_lock:
            cached = self._tokens.get(kind)
            now = time.monotonic()
            if not refresh and cached and cached[1] > now:
                return cached[0]
            base = self._auth if kind == "catalog" else self._hifi
            resp = self._http.get(f"{base}/token")
            resp.raise_for_status()
            token, ttl = self._token_value(resp.json())
            self._tokens[kind] = (token, now + ttl)
            return token

    def _authorized_get(
        self, url: str, kind: str, *, params: dict[str, str | int]
    ) -> httpx.Response:
        for refresh in (False, True):
            token = self._token(kind, refresh=refresh)
            resp = self._http.get(url, params=params, headers={"Authorization": f"Bearer {token}"})
            if resp.status_code != _UNAUTHORIZED:
                resp.raise_for_status()
                return resp
        raise AssertionError("unreachable")

    def search(self, query: str) -> dict[str, Any]:
        resp = self._authorized_get(
            f"{self._catalog}/search/tracks",
            "catalog",
            params={"query": query, "limit": 25, "offset": 0, "countryCode": "US"},
        )
        return dict(resp.json())

    def manifest(self, track_id: str) -> dict[str, Any]:
        if not track_id.isdecimal():
            raise ValueError("invalid TIDAL track ID")
        last_error: httpx.HTTPStatusError | None = None
        for quality in _MANIFEST_QUALITIES:
            try:
                resp = self._authorized_get(
                    f"{self._hifi}/manifests",
                    "playback",
                    params={"id": track_id, "quality": quality},
                )
                return dict(resp.json())
            except httpx.HTTPStatusError as exc:
                last_error = exc
        if last_error is not None:
            raise last_error
        raise AssertionError("TIDAL manifest quality list is empty")

    @staticmethod
    def _safe_tidal_url(url: str) -> str:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        if parsed.scheme != "https" or not (host == "tidal.com" or host.endswith(".tidal.com")):
            raise ValueError("TIDAL manifest referenced an untrusted media URL")
        return url

    @staticmethod
    def _render_segment(template: str, number: int, timestamp: int) -> str:
        rendered = template.replace("$Time$", str(timestamp))

        def number_value(match: re.Match[str]) -> str:
            width = int(match.group(1) or 0)
            return f"{number:0{width}d}" if width else str(number)

        return _NUMBER.sub(number_value, rendered)

    def _segment_urls(self, mpd_url: str) -> list[str]:
        self._safe_tidal_url(mpd_url)
        resp = self._http.get(mpd_url, timeout=60.0)
        resp.raise_for_status()
        root = ElementTree.fromstring(resp.content)
        representations = root.findall(".//{*}Representation")
        flac = [
            representation
            for representation in representations
            if "flac" in str(representation.get("codecs") or "").casefold()
            or "flac" in str(representation.get("id") or "").casefold()
        ]

        def quality(representation: ElementTree.Element) -> tuple[int, int]:
            return (
                int(representation.get("audioSamplingRate") or 0),
                int(representation.get("bandwidth") or 0),
            )

        selected = max(flac, key=quality) if flac else None
        template = (
            selected.find("{*}SegmentTemplate")
            if selected is not None
            else root.find(".//{*}SegmentTemplate")
        )
        if template is None:
            raise ValueError("TIDAL MPD has no segment template")
        initialization = template.get("initialization")
        media = template.get("media")
        if not initialization or not media:
            raise ValueError("TIDAL MPD has incomplete segment URLs")
        urls = [self._safe_tidal_url(initialization)]
        number = int(template.get("startNumber", "1"))
        timestamp = 0
        timeline = template.find("{*}SegmentTimeline")
        if timeline is None:
            raise ValueError("TIDAL MPD has no segment timeline")
        for segment in timeline.findall("{*}S"):
            if segment.get("t") is not None:
                timestamp = int(segment.get("t", "0"))
            duration = int(segment.get("d", "0"))
            repeat = int(segment.get("r", "0"))
            if duration <= 0 or repeat < 0:
                raise ValueError("TIDAL MPD has an unsupported segment timeline")
            for _ in range(repeat + 1):
                url = self._render_segment(media, number, timestamp)
                urls.append(self._safe_tidal_url(url))
                number += 1
                timestamp += duration
        return urls

    @staticmethod
    def _manifest_uri(data: dict[str, Any]) -> str:
        resource = data.get("data") or {}
        attributes = resource.get("attributes") or {}
        if attributes.get("trackPresentation") not in (None, "FULL"):
            raise ValueError("TIDAL track is not available for full playback")
        uri = attributes.get("uri")
        if not uri:
            raise ValueError("TIDAL manifest response has no MPD URI")
        return str(uri)

    def fetch(self, track_id: str, dest: Path) -> None:
        """Download each DASH segment, then losslessly remux it to FLAC."""
        mp4 = dest.with_name(f"{dest.name}.mp4")
        try:
            urls = self._segment_urls(self._manifest_uri(self.manifest(track_id)))
            with mp4.open("wb") as output:
                for url in urls:
                    with self._http.stream("GET", url, timeout=120.0) as resp:
                        resp.raise_for_status()
                        for chunk in resp.iter_bytes(_CHUNK):
                            output.write(chunk)
            self._remux(mp4, dest)
        except Exception:
            dest.unlink(missing_ok=True)
            raise
        finally:
            mp4.unlink(missing_ok=True)

    def close(self) -> None:
        self._http.close()
