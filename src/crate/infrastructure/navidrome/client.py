"""Navidrome (Subsonic API) — just ``startScan``, so a purge or restore shows up in
the player without waiting for Navidrome's own schedule. Token auth: ``t =
md5(password + salt)``; the password itself is never sent."""

from __future__ import annotations

import hashlib
import secrets
from collections.abc import Callable
from typing import Any

GetJson = Callable[[str, dict[str, str]], Any]


def start_scan(base_url: str, user: str, password: str, *, get: GetJson) -> bool:
    """Ask Navidrome to rescan its library; True when it accepted."""
    salt = secrets.token_hex(6)
    token = hashlib.md5((password + salt).encode(), usedforsecurity=False).hexdigest()
    data = get(
        f"{base_url.rstrip('/')}/rest/startScan",
        {"u": user, "t": token, "s": salt, "v": "1.16.1", "c": "crate", "f": "json"},
    )
    reply = (data or {}).get("subsonic-response") or {}
    return reply.get("status") == "ok"
