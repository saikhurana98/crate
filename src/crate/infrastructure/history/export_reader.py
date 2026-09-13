"""Read an uploaded Spotify streaming-history export (a zip or a single JSON file).

The content is sniffed (the zip magic bytes), and the name the client sent is
ignored. Files are loaded one at a time (``ExportFileRef.load``), because a
full history is tens of MB of JSON across a dozen files.
"""

from __future__ import annotations

import json
import re
import zipfile
from pathlib import Path
from typing import IO, Any

_ZIP_MAGIC = b"PK\x03\x04"
# Extended history (Streaming_History_Audio_2019-2021_0.json) and the short
# account-data export (StreamingHistory_music_0.json / StreamingHistory0.json).
# Video history and everything else in the zip is ignored.
_HISTORY = re.compile(
    r"(?:^|/)(?:Streaming_History_Audio_[^/]*|StreamingHistory(?:_music)?_?\d*)\.json$",
    re.IGNORECASE,
)


class ExportFileRef:
    """One history JSON file inside an upload, loaded on demand."""

    def __init__(self, path: Path, member: str | None = None) -> None:
        self._path = path
        self._member = member

    @property
    def name(self) -> str:
        return self._member.rsplit("/", 1)[-1] if self._member else self._path.name

    def load(self) -> list[dict[str, Any]]:
        if self._member is None:
            with self._path.open("rb") as fh:
                return _load(fh, self.name)
        with zipfile.ZipFile(self._path) as zf, zf.open(self._member) as fh:
            return _load(fh, self.name)


def _load(fh: IO[bytes], name: str) -> list[dict[str, Any]]:
    try:
        data = json.load(fh)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError(f"{name}: not valid JSON ({exc})") from exc
    if not isinstance(data, list):
        raise ValueError(f"{name}: expected a JSON list of plays")
    return [e for e in data if isinstance(e, dict)]


def open_export(path: Path) -> list[ExportFileRef]:
    """The history files in an upload, oldest-named first. Raises ValueError for a
    zip with no history files or content that is neither a zip nor JSON."""
    with path.open("rb") as fh:
        head = fh.read(4)
    if head.startswith(_ZIP_MAGIC):
        with zipfile.ZipFile(path) as zf:
            members = sorted(
                i.filename for i in zf.infolist() if not i.is_dir() and _HISTORY.search(i.filename)
            )
        if not members:
            raise ValueError(
                "no Spotify streaming-history files in the zip "
                "(expected Streaming_History_Audio_*.json)"
            )
        return [ExportFileRef(path, m) for m in members]
    stripped = head.lstrip(b"\xef\xbb\xbf \t\r\n")
    if stripped[:1] in (b"[", b"{") or not stripped:
        return [ExportFileRef(path)]
    raise ValueError("upload is neither a zip nor a JSON file")
