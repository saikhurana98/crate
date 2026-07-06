"""slskd REST client for the Tier-2 Soulseek adapter.

Drives a running slskd daemon: search the Soulseek network, enqueue a download,
wait for the transfer, and move the completed file into the output dir. Requires
slskd's downloads directory to be readable by this process (shared mount or same
host). All network/filesystem I/O; excluded from coverage.
"""

from __future__ import annotations

import shutil
import time
from pathlib import Path
from typing import Any

import httpx

from crate.adapters.sources.soulseek import SlskFile

_POLL_S = 2.0


class SlskdClient:  # pragma: no cover - network + filesystem I/O
    def __init__(
        self,
        base_url: str,
        api_key: str,
        downloads_dir: str,
        *,
        search_timeout_s: float = 30.0,
        transfer_timeout_s: float = 300.0,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._downloads = Path(downloads_dir)
        self._search_timeout = search_timeout_s
        self._transfer_timeout = transfer_timeout_s
        self._http = httpx.Client(headers={"X-API-Key": api_key}, timeout=30.0, base_url=self._base)

    def search(self, query: str) -> list[dict[str, Any]]:
        started = self._http.post("/api/v0/searches", json={"searchText": query})
        started.raise_for_status()
        search_id = started.json()["id"]

        deadline = time.monotonic() + self._search_timeout
        while time.monotonic() < deadline:
            state = self._http.get(f"/api/v0/searches/{search_id}").json()
            if state.get("state", "").startswith("Completed") or state.get("fileCount", 0) > 0:
                break
            time.sleep(_POLL_S)

        resp = self._http.get(f"/api/v0/searches/{search_id}/responses")
        resp.raise_for_status()
        return list(resp.json())

    def download(self, file: SlskFile, out_dir: Path) -> Path:
        self._http.post(
            f"/api/v0/transfers/downloads/{file.username}",
            json=[{"filename": file.filename, "size": file.size}],
        ).raise_for_status()

        base = file.filename.replace("\\", "/").rsplit("/", 1)[-1]
        deadline = time.monotonic() + self._transfer_timeout
        while time.monotonic() < deadline:
            transfers = self._http.get(f"/api/v0/transfers/downloads/{file.username}").json()
            if _is_completed(transfers, base):
                break
            if _is_failed(transfers, base):
                raise RuntimeError(f"slskd transfer failed for {base}")
            time.sleep(_POLL_S)
        else:
            raise RuntimeError(f"slskd transfer timed out for {base}")

        src = _find_file(self._downloads, base)
        if src is None:
            raise RuntimeError(f"completed file not found under {self._downloads}: {base}")
        dest = out_dir / base
        shutil.move(str(src), str(dest))
        return dest

    def close(self) -> None:
        self._http.close()


def _iter_files(transfers: Any) -> Any:
    for directory in transfers.get("directories", []):
        yield from directory.get("files", [])


def _is_completed(transfers: Any, base: str) -> bool:
    # slskd terminal states are "Completed, <Succeeded|Errored|Cancelled|TimedOut|Rejected>".
    # Only Succeeded means the file is on disk.
    return any(
        f.get("filename", "").endswith(base) and "Succeeded" in f.get("state", "")
        for f in _iter_files(transfers)
    )


def _is_failed(transfers: Any, base: str) -> bool:
    return any(
        f.get("filename", "").endswith(base)
        and any(s in f.get("state", "") for s in ("Errored", "Cancelled", "Rejected", "TimedOut"))
        for f in _iter_files(transfers)
    )


def _find_file(root: Path, base: str) -> Path | None:
    for path in root.rglob(base):
        if path.is_file():
            return path
    return None
