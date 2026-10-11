"""HTTP client for Antra's authenticated hosted job API."""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

DEFAULT_BASE = "https://antra.hoshi.cfd"
_CHUNK = 64 * 1024
_UNAUTHORIZED = 401


class AntraClient:  # pragma: no cover - network I/O
    def __init__(
        self,
        username: str,
        password: str,
        base_url: str = DEFAULT_BASE,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        poll_interval_s: float = 2.0,
        timeout_s: float = 300.0,
    ) -> None:
        if not username or not password:
            raise ValueError("Antra username and password are required")
        self._username = username
        self._password = password
        self._base = base_url.rstrip("/")
        self._sleep = sleep
        self._poll_interval_s = poll_interval_s
        self._timeout_s = timeout_s
        self._logged_in = False
        self._http = httpx.Client(
            timeout=30.0,
            follow_redirects=True,
            transport=transport or httpx.HTTPTransport(retries=2),
        )

    @staticmethod
    def _detail(resp: httpx.Response) -> str:
        try:
            data = resp.json()
            if isinstance(data, dict) and data.get("detail"):
                return str(data["detail"])
        except ValueError:
            pass
        return f"HTTP {resp.status_code}"

    def login(self) -> None:
        resp = self._http.post(
            f"{self._base}/api/auth/login",
            json={"username": self._username, "password": self._password},
        )
        if not resp.is_success:
            raise RuntimeError(f"Antra login failed: {self._detail(resp)}")
        self._logged_in = True

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        if not self._logged_in:
            self.login()
        resp = self._http.request(method, f"{self._base}{path}", **kwargs)
        if resp.status_code == _UNAUTHORIZED:
            self._logged_in = False
            self.login()
            resp = self._http.request(method, f"{self._base}{path}", **kwargs)
        return resp

    def submit_track(self, track: dict[str, Any]) -> str:
        """Submit one metadata row; Antra resolves it across its source chain."""
        payload = {
            "url": "import://crate",
            "format": "lossless-16",
            "tracks": [track],
            "import_name": "Crate",
            "start_index": 0,
            "end_index": 1,
            "client_packaging": True,
        }
        resp = self._request("POST", "/api/jobs", json=payload)
        if not resp.is_success:
            code = resp.headers.get("X-Antra-Error-Code")
            suffix = f" ({code})" if code else ""
            raise RuntimeError(f"Antra job submission failed{suffix}: {self._detail(resp)}")
        job_id = str(resp.json().get("job_id") or "")
        if not job_id:
            raise RuntimeError("Antra job response did not include a job ID")
        return job_id

    def wait(self, job_id: str) -> dict[str, Any]:
        deadline = time.monotonic() + self._timeout_s
        while time.monotonic() < deadline:
            resp = self._request("GET", f"/api/jobs/{job_id}/status")
            if not resp.is_success:
                raise RuntimeError(f"Antra status failed: {self._detail(resp)}")
            data = dict(resp.json())
            status = data.get("status")
            if status == "complete":
                if int(data.get("done") or 0) < 1:
                    raise RuntimeError(str(data.get("error") or "Antra produced no track"))
                return data
            if status == "failed":
                raise RuntimeError(str(data.get("error") or "Antra download failed"))
            self._sleep(self._poll_interval_s)
        raise TimeoutError("Antra job did not finish before the timeout")

    def fetch_track(self, job_id: str, dest: Path) -> None:
        with self._http.stream(
            "GET", f"{self._base}/api/jobs/{job_id}/tracks/0/download", timeout=120.0
        ) as resp:
            if resp.status_code == _UNAUTHORIZED:
                raise RuntimeError("Antra session expired before track download")
            if not resp.is_success:
                raise RuntimeError(f"Antra track download failed: {self._detail(resp)}")
            with dest.open("wb") as fh:
                for chunk in resp.iter_bytes(_CHUNK):
                    fh.write(chunk)

    def download_track(self, track: dict[str, Any], dest: Path) -> None:
        job_id = self.submit_track(track)
        self.wait(job_id)
        self.fetch_track(job_id, dest)

    def close(self) -> None:
        self._http.close()
