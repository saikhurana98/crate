"""slskd-compatible shim state machine (ADR-0014).

Emulates the subset of slskd's REST API that Explo drives. Explo points
``SLSKD_URL`` at crate and treats it as slskd; each search returns a single
synthetic result from user ``crate``, and enqueuing it runs the crate tier cascade
(injected as ``download``) and writes the FLAC where Explo expects to collect it.

Pure request handling + a small transfer state machine; the actual acquisition
is the injected ``download(query, out_dir) -> Path``. Threading lives in the
server; ``run_download`` is called directly in tests.
"""

from __future__ import annotations

import re
import shutil
import threading
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

Download = Callable[[str, Path], Path]  # (query, out_dir) -> path of the FLAC

_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


class ShimState:
    def __init__(self, download: Download, downloads_dir: Path, api_key: str | None = None) -> None:
        self._download = download
        self._dir = downloads_dir
        self._api_key = api_key
        self._counter = 0
        self._searches: dict[str, str] = {}  # search id -> query
        self._file_query: dict[str, str] = {}  # advertised filename -> query
        self._transfers: dict[str, dict[str, Any]] = {}  # filename -> transfer record
        self._lock = threading.Lock()

    # --- auth ---
    def authorized(self, headers: Mapping[str, str]) -> bool:
        if not self._api_key:
            return True
        got = headers.get("X-API-Key") or headers.get("x-api-key")
        return got == self._api_key

    # --- search ---
    def start_search(self, search_text: str) -> dict[str, Any]:
        with self._lock:
            self._counter += 1
            sid = str(self._counter)
            self._searches[sid] = search_text
        return {
            "id": sid,
            "searchText": search_text,
            "state": "InProgress",
            "fileCount": 0,
            "responseCount": 0,
        }

    def search_state(self, sid: str) -> dict[str, Any]:
        return {
            "id": sid,
            "searchText": self._searches.get(sid, ""),
            "state": "Completed, Succeeded",
            "fileCount": 1,
            "responseCount": 1,
            "isComplete": True,
        }

    def _advertised_filename(self, query: str) -> str:
        return _UNSAFE.sub("_", query) + ".flac"

    def search_responses(self, sid: str) -> list[dict[str, Any]]:
        query = self._searches.get(sid, "")
        filename = self._advertised_filename(query)
        with self._lock:
            self._file_query[filename] = query
        return [
            {
                "username": "crate",
                "hasFreeUploadSlot": True,
                "uploadSpeed": 10_000_000,
                "queueLength": 0,
                "fileCount": 1,
                "files": [
                    {
                        "filename": filename,
                        "size": 40_000_000,
                        "bitRate": 1411,
                        "bitDepth": 16,
                        "sampleRate": 44100,
                        "length": 240,
                        "extension": ".flac",
                        "isLocked": False,
                    }
                ],
            }
        ]

    # --- transfers (download) ---
    def enqueue(self, username: str, files: list[dict[str, Any]]) -> None:
        for f in files:
            filename = str(f["filename"])
            with self._lock:
                self._transfers[filename] = {
                    "filename": filename,
                    "size": int(f.get("size", 0)),
                    "bytesTransferred": 0,
                    "state": "Queued, Remotely",
                }

    def run_download(self, filename: str) -> None:
        """Run the cascade for this file and land the FLAC at downloads_dir/filename."""
        query = self._file_query.get(filename, Path(filename).stem)
        with self._lock:
            self._transfers.setdefault(filename, {"filename": filename, "size": 0})
            self._transfers[filename]["state"] = "InProgress"
        try:
            produced = self._download(query, self._dir)
            dest = self._dir / filename
            dest.parent.mkdir(parents=True, exist_ok=True)
            if produced.resolve() != dest.resolve():
                shutil.move(str(produced), str(dest))
            size = dest.stat().st_size
            with self._lock:
                self._transfers[filename].update(
                    state="Completed, Succeeded", bytesTransferred=size, size=size
                )
        except Exception as exc:  # surface as a failed transfer
            with self._lock:
                self._transfers[filename].update(state="Completed, Errored", exception=str(exc))

    def transfers(self, username: str) -> dict[str, Any]:
        with self._lock:
            files = [dict(rec) for rec in self._transfers.values()]
        return {
            "username": "crate",
            "directories": [{"directory": "crate", "fileCount": len(files), "files": files}],
        }
