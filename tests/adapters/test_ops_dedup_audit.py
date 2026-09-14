"""/api/dedup/audit/*: tag audit, orphans, album-folder duplicates."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from crate.adapters.inbound.ops_api import create_app
from crate.application.identify import AcoustidMatch
from crate.application.library_dedup import GroupRecord, MemberRecord
from crate.application.library_scan import FileAnalysis
from crate.domain.library import TrackTags
from crate.domain.library_scan import DiskEntry, ScanPlan
from crate.infrastructure.sqlite.db import connect
from crate.infrastructure.sqlite.dedup_store import SqliteDedupStore
from crate.infrastructure.sqlite.library_store import SqliteLibraryStore
from crate.infrastructure.sqlite.store import RequestStore

ROOT = Path("/library")


def _analysis(artist: str, title: str) -> FileAnalysis:
    return FileAnalysis(
        codec="flac",
        container="flac",
        lossless=True,
        bitrate=None,
        bit_depth=16,
        sample_rate=44100,
        channels=2,
        duration_s=200.0,
        tags=TrackTags(artist=artist, title=title),
        has_cover=False,
        fp_duration=200,
        fp_compressed="x",
        fp_raw=[1],
    )


def _client(tmp_path: Path, root: Path | None = ROOT) -> tuple[TestClient, dict[str, str]]:
    conn = connect(tmp_path / "crate.db")
    lib = SqliteLibraryStore(conn)
    paths = [
        "A/LP/1.flac",
        "A/LP/2.flac",
        "A/LP (Deluxe)/1.flac",
        "A/LP (Deluxe)/2.flac",
        "Misc/wrong.flac",
    ]
    lib.apply_plan(ScanPlan(new=[DiskEntry(p, 10, 1, None) for p in paths]))
    ids = {f.path: f.id for f in lib.indexed()}
    for p, fid in ids.items():
        lib.save_analysis(
            fid, _analysis("Daft Punk", "Around the World" if "wrong" in p else "Midnight Rain")
        )
        lib.save_lookup(
            fid,
            AcoustidMatch(
                "aid", 0.95, None, "Daft Punk", "Get Lucky" if "wrong" in p else "Midnight Rain"
            ),
        )
    dedup = SqliteDedupStore(conn)
    dedup.replace_groups(
        [
            GroupRecord(
                f"g{n}",
                "acoustid",
                0.97,
                False,
                ids[f"A/LP/{n}.flac"],
                10,
                [
                    MemberRecord(ids[f"A/LP/{n}.flac"], 0, None, [], {}),
                    MemberRecord(ids[f"A/LP (Deluxe)/{n}.flac"], 1, 1.0, ["x"], {}),
                ],
            )
            for n in (1, 2)
        ]
    )
    requests = RequestStore(conn)
    for rel in ("A/LP/1.flac", "Gone/deleted.flac"):
        rid = requests.enqueue(rel)
        requests.complete_as_duplicate(
            rid,
            {
                "path": str(ROOT / rel),
                "size_bytes": 10,
                "codec": "flac",
                "bitrate": None,
                "bit_depth": 16,
                "sample_rate": 44100,
                "duration_s": 200.0,
                "lossless": 1,
                "quality_label": "lossless",
            },
        )
    return TestClient(create_app(requests, dedup=dedup, library_root=root)), ids


def test_tag_audit_flags_the_wrong_song(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    r = client.get("/api/dedup/audit/tags").json()
    assert r["count"] == 1 and r["items"][0]["path"] == "Misc/wrong.flac"
    assert r["items"][0]["disagrees"] == "title" and r["items"][0]["aid_title"] == "Get Lucky"


def test_orphans_both_ways(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    r = client.get("/api/dedup/audit/orphans").json()
    assert r["records_missing"]["count"] == 1
    assert r["records_missing"]["items"][0]["path"] == "/library/Gone/deleted.flac"
    assert (
        r["files_unrecorded"]["count"] == 4 and "A/LP/1.flac" not in r["files_unrecorded"]["items"]
    )


def test_orphans_need_a_library_root(tmp_path: Path) -> None:
    client, _ = _client(tmp_path, root=None)
    assert client.get("/api/dedup/audit/orphans").status_code == 503


def test_album_folder_duplicates(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    folders = client.get("/api/dedup/audit/folders").json()
    assert len(folders) == 1
    f = folders[0]
    assert (
        {f["folder"], f["other"]} == {"A/LP", "A/LP (Deluxe)"}
        and f["shared"] == 2
        and f["ratio"] == 1.0
    )
