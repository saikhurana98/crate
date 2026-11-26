"""Client for the open-source JioSaavn API."""

from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from crate.infrastructure.http import get_json, stream_to

DEFAULT_BASE = "https://saavn.sumit.co"


class SaavnClient:
    def __init__(self, base: str = DEFAULT_BASE) -> None:
        self._base = base.rstrip("/")

    def search(self, query: str) -> list[dict[str, Any]]:  # pragma: no cover - network I/O
        payload = get_json(f"{self._base}/api/search/songs", {"query": query, "limit": 15})
        if payload.get("success") is not True:
            raise RuntimeError("JioSaavn search API returned an error")
        return list(payload["data"]["results"])

    def fetch(self, track_id: str, dest: Path) -> None:  # pragma: no cover - network I/O
        payload = get_json(f"{self._base}/api/songs/{track_id}", {})
        songs = payload.get("data") or []
        if not payload.get("success") or not songs or songs[0].get("id") != track_id:
            raise RuntimeError("JioSaavn track lookup returned no matching result")
        urls: dict[str, str] = {}
        for item in songs[0].get("downloadUrl") or []:
            url = str(item.get("url", ""))
            parsed = urlsplit(url)
            if parsed.scheme == "https" and parsed.netloc == "aac.saavncdn.com":
                urls[str(item.get("quality"))] = url
        for quality in ("320kbps", "160kbps", "96kbps", "48kbps", "12kbps"):
            if quality in urls:
                stream_to(urls[quality], dest)
                return
        raise RuntimeError("JioSaavn returned no supported audio URL")
