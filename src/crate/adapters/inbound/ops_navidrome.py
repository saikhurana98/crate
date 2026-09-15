"""Navidrome HTTP routes (mounted under the authenticated ``/api`` router).

Each logged-in user links **their own** Navidrome account; playlists they import
are then also created in that account (the ``navidrome_push`` job in serve does
the Navidrome calls). The password is checked against Navidrome before it's
stored, is stored encrypted, and is never returned.

- ``GET /navidrome``: whether it's configured, the linked account, the mirrors;
- ``PUT /navidrome/account`` / ``DELETE /navidrome/account``: link or unlink;
- ``POST /navidrome/account/sso``: "Sign in with Navidrome" when Navidrome sits
  behind the same SSO as crate. Crate asks Navidrome, through its trusted-header
  sign-in, for the account matching the username crate's own SSO asserted, and
  keeps the token it issues. No password is involved, and a user can only link
  their own name;
- ``PUT /navidrome/playlists/{spotify_id}``: mirror a playlist, or stop;
- ``POST /navidrome/push``: push this user's mirrors now.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

from fastapi import APIRouter, Body, HTTPException, Request

from crate.adapters.inbound.ops_jobs import JobsPort
from crate.adapters.inbound.ops_sync import _logged_in
from crate.domain.jobs import JobKind
from crate.domain.navidrome import NavidromeLogin
from crate.infrastructure.navidrome.client import NavidromeError

LIKED_NAME = "Liked Songs"
_MAX_USERNAME = 200
_MAX_PASSWORD = 1024

VerifyFn = Callable[[str, str], None]  # (username, password); raises NavidromeError


class NavidromePort(Protocol):
    def account(self, user_id: str) -> dict[str, Any] | None: ...
    def set_account(self, user_id: str, username: str, password: str) -> None: ...
    def set_login(self, user_id: str, login: NavidromeLogin) -> None: ...
    def delete_account(self, user_id: str) -> bool: ...
    def links(self, user_id: str, *, enabled_only: bool = False) -> list[dict[str, Any]]: ...
    def link(self, user_id: str, spotify_id: str, name: str) -> None: ...
    def set_enabled(self, user_id: str, spotify_id: str, on: bool) -> bool: ...


class NavidromeSsoPort(Protocol):
    """Navidrome behind SSO: the SSO username for a crate user, and a checked
    trusted-header sign-in for it."""

    def username_of(self, user_id: str) -> str | None: ...
    def login(self, username: str) -> NavidromeLogin: ...


def _push(jobs: JobsPort | None, user_id: str, playlist_id: str | None = None) -> str | None:
    if jobs is None:
        return None
    params = {"playlist_id": playlist_id} if playlist_id else {}
    job_id, _ = jobs.enqueue(
        JobKind.NAVIDROME_PUSH, triggered_by="manual", user_id=user_id, params=params
    )
    return job_id


def mirror_import(
    nd: NavidromePort, jobs: JobsPort | None, user_id: str, spotify_id: str, name: str
) -> bool:
    """After an import: mirror the playlist into the user's Navidrome account, if
    they've linked one. True when it will be mirrored."""
    if nd.account(user_id) is None:
        return False
    nd.link(user_id, spotify_id, name)
    _push(jobs, user_id, spotify_id)
    return True


def navidrome_router(
    nd: NavidromePort,
    jobs: JobsPort | None = None,
    *,
    verify: VerifyFn | None = None,
    public_url: str | None = None,
    sso: NavidromeSsoPort | None = None,
) -> APIRouter:
    router = APIRouter()

    def _uid(request: Request) -> str:
        return _logged_in(request, "link Navidrome")

    def _linked(uid: str) -> None:
        if nd.account(uid) is None:
            raise HTTPException(status_code=409, detail="link your Navidrome account first")

    @router.get("/navidrome")
    def overview(request: Request) -> dict[str, Any]:
        uid = _uid(request)
        return {
            "configured": verify is not None,
            "public_url": public_url.rstrip("/") if public_url else None,
            "account": nd.account(uid),
            "playlists": nd.links(uid),
            # "Sign in with Navidrome" is on offer: Navidrome is behind SSO and
            # this user signed in to crate with SSO (so crate knows their name).
            "sso": verify is not None and sso is not None and sso.username_of(uid) is not None,
        }

    @router.put("/navidrome/account")
    def link_account(request: Request, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        uid = _uid(request)
        if verify is None:
            raise HTTPException(status_code=503, detail="Navidrome is not configured")
        username = str(body.get("username") or "").strip()
        password = body.get("password")
        if not username or not isinstance(password, str) or not password:
            raise HTTPException(status_code=400, detail="username and password are required")
        if len(username) > _MAX_USERNAME or len(password) > _MAX_PASSWORD:
            raise HTTPException(status_code=400, detail="username or password is too long")
        try:
            verify(username, password)
        except NavidromeError as exc:
            if exc.bad_credentials:
                raise HTTPException(
                    status_code=400, detail="Navidrome rejected that username or password"
                ) from exc
            raise HTTPException(status_code=502, detail=f"Navidrome said: {exc}") from exc
        except Exception as exc:  # network: don't store what we couldn't check
            raise HTTPException(status_code=502, detail="couldn't reach Navidrome") from exc
        nd.set_account(uid, username, password)
        _push(jobs, uid)  # re-push anything already mirrored, e.g. after a new password
        return {"account": nd.account(uid)}

    @router.post("/navidrome/account/sso")
    def link_with_sso(request: Request) -> dict[str, Any]:
        uid = _uid(request)
        if verify is None or sso is None:
            raise HTTPException(status_code=503, detail="Navidrome SSO sign-in is not configured")
        username = sso.username_of(uid)
        if username is None:
            raise HTTPException(
                status_code=409, detail="sign in to crate with SSO first, then link Navidrome"
            )
        try:
            login = sso.login(username)
        except NavidromeError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=502, detail="couldn't reach Navidrome") from exc
        nd.set_login(uid, login)
        _push(jobs, uid)
        return {"account": nd.account(uid)}

    @router.delete("/navidrome/account")
    def unlink_account(request: Request) -> dict[str, Any]:
        return {"removed": nd.delete_account(_uid(request))}

    @router.put("/navidrome/playlists/{spotify_id}")
    def mirror(
        request: Request, spotify_id: str, body: dict[str, Any] = Body(...)
    ) -> dict[str, Any]:
        uid = _uid(request)
        on = body.get("mirror")
        if not isinstance(on, bool):
            raise HTTPException(status_code=400, detail="mirror must be true or false")
        _linked(uid)
        job = None
        if on:
            name = str(body.get("name") or "").strip()
            nd.link(uid, spotify_id, name or (LIKED_NAME if spotify_id == "liked" else spotify_id))
            job = _push(jobs, uid, spotify_id)
        elif not nd.set_enabled(uid, spotify_id, False):
            raise HTTPException(status_code=404, detail="that playlist isn't mirrored")
        row = next(p for p in nd.links(uid) if p["spotify_id"] == spotify_id)
        return row | {"job": job}

    @router.post("/navidrome/push")
    def push_now(request: Request) -> dict[str, Any]:
        uid = _uid(request)
        _linked(uid)
        return {"job": _push(jobs, uid)}

    return router
