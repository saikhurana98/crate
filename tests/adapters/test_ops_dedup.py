"""/api/dedup/* over a real dedup store (single-user mode)."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from crate.adapters.inbound.ops_api import create_app
from crate.application.library_dedup import GroupRecord, MemberRecord
from crate.domain.jobs import JobKind
from crate.domain.library_scan import DiskEntry, ScanPlan
from crate.infrastructure.sqlite.db import connect
from crate.infrastructure.sqlite.dedup_store import SqliteDedupStore
from crate.infrastructure.sqlite.job_store import SqliteJobStore
from crate.infrastructure.sqlite.library_store import SqliteLibraryStore
from crate.infrastructure.sqlite.store import RequestStore


def _client(tmp_path: Path) -> tuple[TestClient, dict[str, str]]:
    conn = connect(tmp_path / "crate.db")
    lib = SqliteLibraryStore(conn)
    lib.apply_plan(
        ScanPlan(new=[DiskEntry(p, 1000, 1, None) for p in ("a.flac", "b.mp3", "c.flac", "d.flac")])
    )
    ids = {f.path: f.id for f in lib.indexed()}
    store = SqliteDedupStore(conn)

    def member(path: str, rank: int) -> MemberRecord:
        reasons = [] if rank == 0 else ["lossless beats lossy"]
        return MemberRecord(
            ids[path], rank, None if rank == 0 else 0.99, reasons, {"canonical": rank == 0}
        )

    store.replace_groups(
        [
            GroupRecord(
                "g1",
                "acoustid",
                0.97,
                False,
                ids["a.flac"],
                1000,
                [member("a.flac", 0), member("b.mp3", 1)],
            ),
            GroupRecord(
                "g2", "fp", 0.93, True, ids["c.flac"], 0, [member("c.flac", 0), member("d.flac", 1)]
            ),
        ]
    )
    jobs = SqliteJobStore(conn)
    jobs.enqueue(JobKind.DEDUP_SCAN, triggered_by="schedule")
    return TestClient(create_app(RequestStore(conn), dedup=store, jobs=jobs)), ids


def test_overview_and_groups(tmp_path: Path) -> None:
    client, ids = _client(tmp_path)
    ov = client.get("/api/dedup/overview").json()
    assert (ov["files"], ov["groups"], ov["variant_groups"], ov["duplicate_files"]) == (4, 2, 1, 1)
    assert ov["reclaimable_bytes"] == 1000 and ov["last_scan"]["status"] == "queued"
    groups = client.get("/api/dedup/groups").json()
    assert [g["id"] for g in groups] == ["g1", "g2"]  # variants last
    keeper, other = groups[0]["members"]
    assert keeper["is_keeper"] and keeper["id"] == ids["a.flac"] and keeper["canonical"] is True
    assert other["reasons"] == ["lossless beats lossy"] and other["score_vs_keeper"] == 0.99
    assert [
        g["id"] for g in client.get("/api/dedup/groups", params={"variants": "true"}).json()
    ] == ["g2"]
    assert client.get("/api/dedup/groups", params={"limit": 0}).status_code == 422


def test_ignore(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    r = client.post("/api/dedup/groups/g1/ignore")
    assert r.status_code == 200 and r.json()["ignored_pairs"] == 1
    assert [g["id"] for g in client.get("/api/dedup/groups").json()] == ["g2"]
    assert client.post("/api/dedup/groups/g1/ignore").status_code == 404
