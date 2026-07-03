"""Minimal stdlib HTTP server exposing the slskd shim (ADR-0014).

Routes the slskd endpoints Explo drives to :class:`ShimState`. Enqueue spawns a
background thread that runs the cascade. No framework dep — a threaded stdlib
server is plenty for a LAN shim. All I/O; excluded from coverage.
"""

from __future__ import annotations

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from lbmd.adapters.inbound.slskd_shim import ShimState

_SEARCH = re.compile(r"^/api/v0/searches/([^/]+)$")
_RESPONSES = re.compile(r"^/api/v0/searches/([^/]+)/responses$")
_TRANSFERS = re.compile(r"^/api/v0/transfers/downloads/([^/]+)$")


class _Handler(BaseHTTPRequestHandler):  # pragma: no cover - network I/O
    server: _ShimServer

    def log_message(self, *args: Any) -> None:
        pass  # quiet

    def _send(self, status: int, obj: Any) -> None:
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _auth(self) -> bool:
        return self.server.shim.authorized(dict(self.headers.items()))

    def _body(self) -> Any:
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b""
        return json.loads(raw) if raw else None

    def do_GET(self) -> None:
        if not self._auth():
            return self._send(401, {"error": "unauthorized"})
        path = self.path.split("?", 1)[0]
        shim = self.server.shim
        if path in ("/api/v0/session", "/api/v0/application", "/health"):
            return self._send(200, {"server": {"state": "Connected, LoggedIn"}})
        if m := _RESPONSES.match(path):
            return self._send(200, shim.search_responses(m.group(1)))
        if m := _SEARCH.match(path):
            return self._send(200, shim.search_state(m.group(1)))
        if m := _TRANSFERS.match(path):
            return self._send(200, shim.transfers(m.group(1)))
        self._send(404, {"error": "not found"})

    def do_POST(self) -> None:
        if not self._auth():
            return self._send(401, {"error": "unauthorized"})
        path = self.path.split("?", 1)[0]
        shim = self.server.shim
        if path == "/api/v0/searches":
            body = self._body() or {}
            return self._send(200, shim.start_search(str(body.get("searchText", ""))))
        if m := _TRANSFERS.match(path):
            username = m.group(1)
            files = self._body() or []
            shim.enqueue(username, files)
            for f in files:
                threading.Thread(
                    target=shim.run_download, args=(str(f["filename"]),), daemon=True
                ).start()
            return self._send(201, {"enqueued": len(files)})
        self._send(404, {"error": "not found"})


class _ShimServer(ThreadingHTTPServer):  # pragma: no cover
    def __init__(self, addr: tuple[str, int], shim: ShimState) -> None:
        super().__init__(addr, _Handler)
        self.shim = shim


def run_server(shim: ShimState, host: str, port: int) -> None:  # pragma: no cover - network I/O
    _ShimServer((host, port), shim).serve_forever()
