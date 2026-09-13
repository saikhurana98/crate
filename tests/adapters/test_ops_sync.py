"""/api/sync/* over real stores (TestClient)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from crate.adapters.inbound.ops_api import create_app
from crate.application.affinity import TierMember
from crate.application.auth import SpotifyProfile, User
from crate.application.library_sync import LibraryIndex, LibrarySyncService
from crate.domain.affinity import AffinityTier
from crate.domain.jobs import TERMINAL, JobKind, JobStatus
from crate.domain.listening import merge_key
from crate.infrastructure.sqlite.affinity_store import SqliteAffinityStore
from crate.infrastructure.sqlite.db import connect, now_iso
from crate.infrastructure.sqlite.job_store import SqliteJobStore
from crate.infrastructure.sqlite.store import RequestStore
from crate.infrastructure.sqlite.sync_store import SqlitePlaylistSyncStore, SqliteSyncQueue
from crate.infrastructure.sqlite.user_store import SqliteUserRepository

COOKIE = {"crate_session": "ok"}


class _Auth:
    def __init__(self, user: User) -> None:
        self.user = user

    def resolve_session(self, raw_token: str) -> User | None:
        return self.user if raw_token == "ok" else None

    def login_url(self, state: str) -> str:
        return ""

    def complete_login(self, code: str) -> tuple[User, str]:
        raise NotImplementedError

    def logout(self, raw_token: str) -> None: ...


class _Env:
    def __init__(self, tmp_path: Path, preview: Any = None) -> None:
        conn = connect(tmp_path / "crate.db")
        self.user = SqliteUserRepository(conn).upsert_spotify_user(SpotifyProfile("sp1", "Sai"))
        self.jobs = SqliteJobStore(conn)
        self.playlists = SqlitePlaylistSyncStore(conn)
        self.affinity = SqliteAffinityStore(conn)
        self.queue = SqliteSyncQueue(conn, RequestStore(conn))
        self.index = LibraryIndex(frozenset(), frozenset({merge_key("A", "One")}))
        service = LibrarySyncService(self.playlists, self.affinity, self.queue, lambda: self.index)
        app = create_app(
            RequestStore(conn),
            auth=_Auth(self.user),
            jobs=self.jobs,
            sync=service,
            sync_preview=preview,
        )
        self.client = TestClient(app)
        uid = self.user.id
        mine = {"spotify_id": "p1", "name": "Mine", "owned": True}
        self.playlists.save_playlist(uid, mine, changed=True)
        self.playlists.set_items(
            uid, "p1", [{"artist": "A", "title": "One"}, {"artist": "B", "title": "Two"}]
        )
        dw = {"spotify_id": "dw", "name": "DW", "owner_id": "spotify", "generated": True}
        self.playlists.save_playlist(uid, dw, changed=True)
        self.playlists.set_items(uid, "dw", [{"artist": "C", "title": "Three"}])
        member = TierMember(AffinityTier.CONSTANT, merge_key("A", "Ever"), "id1", "A", "Ever", 3.0)
        self.affinity.apply_members(uid, AffinityTier.CONSTANT, [member])


def test_sync_routes_need_a_login(tmp_path: Path) -> None:
    env = _Env(tmp_path)
    assert env.client.get("/api/sync/overview").status_code == 401


def test_overview_reports_tiers_playlists_and_schedule(tmp_path: Path) -> None:
    env = _Env(tmp_path)
    r = env.client.get("/api/sync/overview", cookies=COOKIE).json()
    assert r["last_sync"] is None and r["running"] is None and r["next_run_at"] is None
    tiers = {t["tier"]: t for t in r["tiers"]}
    assert (tiers[1]["count"], tiers[1]["in_library"]) == (2, 1)  # DW isn't tier 1
    assert (tiers[2]["count"], tiers[3]["count"]) == (1, 0) and tiers[2]["label"]
    assert tiers[2]["auto_sync"] is False and tiers[2]["approximate"] is False
    assert {p["spotify_id"]: p["auto_sync"] for p in r["playlists"]} == {"p1": False, "dw": False}

    jid, _ = env.jobs.enqueue(JobKind.LIBRARY_SYNC, triggered_by="schedule")
    ok = next(s for s in JobStatus if s in TERMINAL and s.value not in ("failed", "cancelled"))
    env.jobs._conn.execute(
        "UPDATE jobs SET status=?, finished_at=? WHERE id=?", (ok.value, now_iso(), jid)
    )
    running, _ = env.jobs.enqueue(JobKind.LIBRARY_SYNC, triggered_by="manual")
    r = env.client.get("/api/sync/overview", cookies=COOKIE).json()
    assert r["last_sync"]["id"] == jid and r["running"]["id"] == running
    assert r["next_run_at"] and r["interval_s"] == 7200.0


def test_tier_toggle_baselines_unless_backfilling(tmp_path: Path) -> None:
    env = _Env(tmp_path)
    c = env.client
    r = c.put("/api/sync/tiers/2", json={"auto_sync": True}, cookies=COOKIE).json()
    assert (r["missing"], r["baselined"], r["auto_sync"]) == (1, 1, True)
    again = c.put("/api/sync/tiers/2", json={"auto_sync": True}, cookies=COOKIE).json()
    assert again["missing"] == 0  # already on: nothing to decide
    c.put("/api/sync/tiers/2", json={"auto_sync": False}, cookies=COOKIE)
    body = {"auto_sync": True, "backfill": True}
    r = c.put("/api/sync/tiers/2", json=body, cookies=COOKIE).json()
    assert (r["missing"], r["baselined"]) == (1, 0)


def test_tier1_toggle_baselines_inheriting_playlists(tmp_path: Path) -> None:
    env = _Env(tmp_path)
    r = env.client.put("/api/sync/tiers/1", json={"auto_sync": True}, cookies=COOKIE).json()
    assert (r["missing"], r["baselined"]) == (1, 1)  # Two (One is in the library; DW excluded)
    overview = env.client.get("/api/sync/overview", cookies=COOKIE).json()
    modes = {p["spotify_id"]: p["auto_sync"] for p in overview["playlists"]}
    assert modes == {"p1": True, "dw": False}


def test_playlist_mode_toggle(tmp_path: Path) -> None:
    env = _Env(tmp_path)
    c = env.client
    r = c.put("/api/sync/playlists/dw", json={"mode": "on"}, cookies=COOKIE).json()
    assert (r["auto_sync"], r["missing"], r["baselined"]) == (True, 1, 1)
    body = {"mode": "on", "backfill": True}
    r = c.put("/api/sync/playlists/p1", json=body, cookies=COOKIE).json()
    assert (r["missing"], r["baselined"]) == (1, 0)
    bad = c.put("/api/sync/playlists/p1", json={"mode": "maybe"}, cookies=COOKIE)
    assert bad.status_code == 400
    gone = c.put("/api/sync/playlists/nope", json={"mode": "on"}, cookies=COOKIE)
    assert gone.status_code == 404


def test_members_missing_and_validation(tmp_path: Path) -> None:
    env = _Env(tmp_path)
    c = env.client
    rows = c.get("/api/sync/tiers/2/members", cookies=COOKIE).json()
    assert [(m["title"], m["in_library"]) for m in rows] == [("Ever", False)]
    assert c.get("/api/sync/tiers/1/members", cookies=COOKIE).status_code == 404
    assert c.get("/api/sync/tiers/2/members?limit=0", cookies=COOKIE).status_code == 422
    for scope, n in (("core", 1), ("affinity:2", 1), ("playlist:dw", 1), ("affinity:3", 0)):
        got = c.get("/api/sync/missing", params={"scope": scope}, cookies=COOKIE).json()
        assert got["missing"] == n, scope
    assert c.get("/api/sync/missing", params={"scope": "bogus"}, cookies=COOKIE).status_code == 400
    assert c.put("/api/sync/tiers/7", json={"auto_sync": True}, cookies=COOKIE).status_code == 404
    assert c.put("/api/sync/tiers/2", json={"auto_sync": "yes"}, cookies=COOKIE).status_code == 400


CLIP = "https://cdnt-preview.dzcdn.net/api/1/1/a/b/c/0/abc.mp3"
SID = "4uLU6hMCjMI75M1A2tKUQC"


def test_preview_redirects_to_a_fresh_clip(tmp_path: Path) -> None:
    calls: list[str] = []

    def preview(sid: str) -> str:
        calls.append(sid)
        return CLIP

    env = _Env(tmp_path, preview)
    r = env.client.get(f"/api/sync/preview/{SID}", cookies=COOKIE, follow_redirects=False)
    assert r.status_code == 307 and r.headers["location"] == CLIP and calls == [SID]
    env.client.get(f"/api/sync/preview/{SID}", cookies=COOKIE, follow_redirects=False)
    assert calls == [SID, SID]  # resolved every time: Deezer clip URLs expire


def test_preview_failures_map_to_status_codes(tmp_path: Path) -> None:
    def preview(sid: str) -> str | None:
        if sid == "bad":
            raise ValueError("not a Spotify track id")
        if sid == "down":
            raise RuntimeError("deezer down")
        return None

    env = _Env(tmp_path, preview)

    def status(sid: str) -> int:
        return env.client.get(
            f"/api/sync/preview/{sid}", cookies=COOKIE, follow_redirects=False
        ).status_code

    assert (status(SID), status("bad"), status("down")) == (404, 400, 502)
    assert env.client.get(f"/api/sync/preview/{SID}", follow_redirects=False).status_code == 401


def test_preview_503_when_not_configured(tmp_path: Path) -> None:
    env = _Env(tmp_path)
    r = env.client.get(f"/api/sync/preview/{SID}", cookies=COOKIE, follow_redirects=False)
    assert r.status_code == 503
