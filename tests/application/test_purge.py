"""Purge / restore / empty over a real temp library, real SQLite, real journal."""

from __future__ import annotations

import errno
import hashlib
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from crate.application.jobs import JobContext
from crate.application.library_dedup import GroupRecord, MemberRecord
from crate.application.library_scan import FileAnalysis
from crate.application.purge import reconcile_pending, run_empty, run_purge, run_restore
from crate.domain.jobs import JobKind
from crate.domain.library import TrackTags
from crate.domain.library_scan import DiskEntry, ScanPlan
from crate.domain.purge import PurgeItem, free_path, refuse_reason, restored_name, trash_relpath
from crate.infrastructure.fs.library_fs import LibraryFs
from crate.infrastructure.navidrome.client import start_scan
from crate.infrastructure.sqlite.db import connect
from crate.infrastructure.sqlite.dedup_store import SqliteDedupStore
from crate.infrastructure.sqlite.job_store import SqliteJobStore
from crate.infrastructure.sqlite.library_store import SqliteLibraryStore
from crate.infrastructure.sqlite.store import RequestStore
from crate.infrastructure.sqlite.trash_store import SqliteTrashStore

DAY = "2026-09-14"


class World:
    def __init__(self, tmp_path: Path) -> None:
        self.root = tmp_path / "library"
        self.root.mkdir()
        self.conn = connect(tmp_path / "crate.db")
        self.lib = SqliteLibraryStore(self.conn)
        self.dedup = SqliteDedupStore(self.conn)
        self.store = SqliteTrashStore(self.conn, self.root)
        self.fs = LibraryFs(self.root)
        self.requests = RequestStore(self.conn)
        self.jobs = SqliteJobStore(self.conn)
        self.jobs.enqueue(JobKind.DEDUP_PURGE, triggered_by="manual")
        job = self.jobs.claim_next("mutate")
        assert job is not None
        self.ctx = JobContext(job, self.jobs)
        self.rescans = 0

    def add(self, rel: str, data: bytes, *, lossless: bool) -> str:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        st = path.stat()
        self.lib.apply_plan(ScanPlan(new=[DiskEntry(rel, st.st_size, st.st_mtime_ns, st.st_ino)]))
        fid = next(f.id for f in self.lib.indexed() if f.path == rel)
        self.lib.save_analysis(
            fid,
            FileAnalysis(
                codec="flac" if lossless else "mp3",
                container="flac" if lossless else "mp3",
                lossless=lossless,
                bitrate=None if lossless else 320_000,
                bit_depth=24 if lossless else None,
                sample_rate=48000 if lossless else 44100,
                channels=2,
                duration_s=200.0,
                tags=TrackTags(artist="A", title="Song"),
                has_cover=True,
                fp_duration=200,
                fp_compressed="x",
                fp_raw=[1, 2, 3],
            ),
        )
        return fid

    def item(self, fid: str, keeper: str, group: str | None = "g1") -> PurgeItem:
        row = self.store.file_row(fid)
        assert row is not None
        return PurgeItem(fid, keeper, group, int(row["size"]), int(row["mtime_ns"]))

    def download(self, rel: str) -> str:
        rid = self.requests.enqueue("A Song")
        self.requests.complete_as_duplicate(
            rid,
            {
                "path": str(self.root / rel),
                "size_bytes": 3,
                "codec": "mp3",
                "bitrate": 320_000,
                "bit_depth": None,
                "sample_rate": 44100,
                "duration_s": 200.0,
                "lossless": 0,
                "quality_label": "lossy mp3 320 kbps",
            },
        )
        return rid

    def purge(self, items: list[PurgeItem]) -> dict[str, Any]:
        def rescan() -> None:
            self.rescans += 1

        return run_purge(self.ctx, items, store=self.store, fs=self.fs, day=DAY, rescan=rescan)

    def codes(self) -> list[str]:
        return [
            r[0]
            for r in self.conn.execute(
                "SELECT code FROM job_events WHERE job_id=?", (self.ctx.job.id,)
            )
        ]


def _group(w: World, keep: str, *others: str) -> None:
    members = [MemberRecord(keep, 0, None, [], {})] + [
        MemberRecord(o, i, 1.0, ["lossless beats lossy"], {}) for i, o in enumerate(others, 1)
    ]
    w.dedup.replace_groups([GroupRecord("g1", "acoustid", 0.97, False, keep, 6, members)])


def test_purge_moves_to_trash_and_repoints_downloads_to_the_keeper(tmp_path: Path) -> None:
    w = World(tmp_path)
    keep = w.add("A/LP/01 Song.flac", b"FLACDATA", lossless=True)
    mp3 = w.add("dupes/Song.mp3", b"mp3", lossless=False)
    rid = w.download("dupes/Song.mp3")
    _group(w, keep, mp3)
    s = w.purge([w.item(mp3, keep)])
    assert (s["trashed"], s["skipped"], s["failed"], s["freed_bytes"]) == (1, 0, 0, 3)
    assert not (w.root / "dupes").exists()  # emptied folder pruned
    assert (w.root / ".crate-trash" / DAY / "dupes" / "Song.mp3").read_bytes() == b"mp3"
    assert (w.root / ".crate-trash" / ".ndignore").exists()
    dl = w.conn.execute(
        "SELECT path, lossless, codec, quality_label, bit_depth FROM downloads WHERE request_id=?",
        (rid,),
    ).fetchone()
    assert (
        dl["path"] == str(w.root / "A/LP/01 Song.flac")
        and dl["lossless"] == 1
        and dl["codec"] == "flac"
    )
    assert dl["bit_depth"] == 24 and "24-bit" in dl["quality_label"]
    assert w.store.file_row(mp3) is None  # marked missing in the index
    assert w.dedup.groups() == []  # the group dissolved (one copy left)
    assert w.rescans == 1


def test_changed_inflight_and_keeper_problems_are_refused(tmp_path: Path) -> None:
    w = World(tmp_path)
    keep = w.add("keep.flac", b"K", lossless=True)
    changed = w.add("changed.mp3", b"C", lossless=False)
    busy = w.add("busy.mp3", b"B", lossless=False)
    items = [w.item(changed, keep), w.item(busy, keep), w.item(keep, keep)]
    (w.root / "changed.mp3").write_bytes(b"CHANGED")  # edited after review
    rid = w.requests.enqueue("busy")
    w.conn.execute(
        "INSERT INTO review_candidates(request_id, source, ref, title, artist, is_upgrade, "
        "target_path, created_at) VALUES (?, 'qobuz', '1', 't', 'a', 1, ?, 'now')",
        (rid, str(w.root / "busy.mp3")),
    )
    s = w.purge(items)
    assert (s["trashed"], s["skipped"]) == (0, 3) and w.rescans == 0
    msgs = [
        r[0] for r in w.conn.execute("SELECT message FROM job_events WHERE code='purge_skipped'")
    ]
    assert any("changed since you reviewed" in m for m in msgs)
    assert any("download is about to replace" in m for m in msgs)
    assert any("copy being kept" in m for m in msgs)
    (w.root / "keep.flac").unlink()
    w2 = w.purge([w.item(busy, keep)])
    assert w2["skipped"] == 1  # keeper gone from disk


def test_cross_device_stops_and_db_failure_moves_the_file_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = World(tmp_path)
    keep = w.add("keep.flac", b"K", lossless=True)
    a = w.add("a.mp3", b"A", lossless=False)
    b = w.add("b.mp3", b"B", lossless=False)

    def exdev(src: Any, dst: Any) -> None:
        raise OSError(errno.EXDEV, "Invalid cross-device link")

    monkeypatch.setattr(os, "rename", exdev)
    s = w.purge([w.item(a, keep), w.item(b, keep)])
    assert (s["trashed"], s["failed"]) == (
        0,
        1,
    ) and "cross_device" in w.codes()  # stopped at the first
    monkeypatch.undo()

    def broken(*args: Any, **kw: Any) -> None:
        raise RuntimeError("disk I/O error")

    monkeypatch.setattr(w.store, "commit_purge", broken)
    s = w.purge([w.item(b, keep)])
    assert s["failed"] == 1 and (w.root / "b.mp3").read_bytes() == b"B"  # back where it was
    states = [r[0] for r in w.conn.execute("SELECT state FROM trash_items ORDER BY created_at")]
    assert states == ["failed", "failed"]


def test_restore_puts_it_back_or_beside_a_newcomer(tmp_path: Path) -> None:
    w = World(tmp_path)
    keep = w.add("keep.flac", b"K", lossless=True)
    a = w.add("x/a.mp3", b"A", lossless=False)
    b = w.add("y/b.mp3", b"B", lossless=False)
    w.purge([w.item(a, keep), w.item(b, keep)])
    ids = {r["original_path"]: r["id"] for r in w.store.trashed()}
    (w.root / "y").mkdir()
    (w.root / "y" / "b.mp3").write_bytes(b"NEW")  # something new took the path
    s = run_restore(w.ctx, list(ids.values()), store=w.store, fs=w.fs)
    assert s["restored"] == 2
    assert (w.root / "x" / "a.mp3").read_bytes() == b"A"
    assert (w.root / "y" / "b (restored-1).mp3").read_bytes() == b"B"
    assert w.store.file_row(a)["path"] == "x/a.mp3"  # type: ignore[index]
    assert w.store.file_row(b)["path"] == "y/b (restored-1).mp3"  # type: ignore[index]
    assert w.store.trashed() == []


def test_empty_honours_retention_or_takes_everything(tmp_path: Path) -> None:
    w = World(tmp_path)
    keep = w.add("keep.flac", b"K", lossless=True)
    a = w.add("a.mp3", b"A", lossless=False)
    b = w.add("b.mp3", b"B", lossless=False)
    w.purge([w.item(a, keep), w.item(b, keep)])
    old = (datetime.now(UTC) - timedelta(days=40)).isoformat()
    w.conn.execute("UPDATE trash_items SET trashed_at=? WHERE original_path='a.mp3'", (old,))
    s = run_empty(
        w.ctx, store=w.store, fs=w.fs, everything=False, retention_days=30, now=datetime.now(UTC)
    )
    assert s["purged"] == 1 and not (w.root / ".crate-trash" / DAY / "a.mp3").exists()
    assert (w.root / ".crate-trash" / DAY / "b.mp3").exists()
    s = run_empty(
        w.ctx, store=w.store, fs=w.fs, everything=True, retention_days=30, now=datetime.now(UTC)
    )
    assert s["purged"] == 1 and w.store.trashed() == []


def test_reconcile_moves_an_interrupted_purge_back(tmp_path: Path) -> None:
    w = World(tmp_path)
    keep = w.add("keep.flac", b"K", lossless=True)
    a = w.add("a.mp3", b"A", lossless=False)
    item = w.item(a, keep)
    tid = w.store.begin(item, "a.mp3", trash_relpath("a.mp3", DAY), "job", None)
    w.fs.ensure_trash_root()
    w.fs.move("a.mp3", trash_relpath("a.mp3", DAY))  # crash right after the rename
    assert reconcile_pending(w.ctx, w.store, w.fs) == 1
    assert (w.root / "a.mp3").read_bytes() == b"A"
    state = w.conn.execute("SELECT state, error FROM trash_items WHERE id=?", (tid,)).fetchone()
    assert state["state"] == "failed" and "moved back" in state["error"]


def test_library_fs_stays_inside_the_root(tmp_path: Path) -> None:
    fs = LibraryFs(tmp_path / "library")
    (tmp_path / "library").mkdir()
    for bad in ("../x.flac", "/etc/passwd", ""):
        with pytest.raises(ValueError):
            fs.stat(bad)
    (tmp_path / "library" / "a.flac").write_bytes(b"1")
    (tmp_path / "library" / "b.flac").write_bytes(b"2")
    with pytest.raises(FileExistsError):
        fs.move("a.flac", "b.flac")


def test_pure_rules() -> None:
    assert trash_relpath("A/B/c.flac", DAY) == f".crate-trash/{DAY}/A/B/c.flac"
    assert restored_name("A/c.flac", 2) == "A/c (restored-2).flac"
    assert (
        free_path("a.flac", lambda p: p in {"a.flac", "a (restored-1).flac"})
        == "a (restored-2).flac"
    )
    item = PurgeItem("f", "k", None, 10, 5)
    assert PurgeItem.from_dict(item.to_dict()) == item
    ok = {
        "file_row": {"x": 1},
        "keeper_row": {"x": 1},
        "on_disk": (10, 5),
        "keeper_on_disk": True,
        "inflight": False,
    }
    assert refuse_reason(item, **ok) is None  # type: ignore[arg-type]
    assert refuse_reason(item, **(ok | {"on_disk": None})) == "the file is already gone"  # type: ignore[arg-type]
    assert refuse_reason(item, **(ok | {"file_row": None})) == "it's no longer in the library index"  # type: ignore[arg-type]


def test_navidrome_start_scan_uses_token_auth() -> None:
    seen: dict[str, Any] = {}

    def get(url: str, params: dict[str, str]) -> Any:
        seen["url"], seen["params"] = url, params
        return {"subsonic-response": {"status": "ok"}}

    assert start_scan("https://music.example/", "sai", "pw", get=get)
    p = seen["params"]
    assert seen["url"] == "https://music.example/rest/startScan" and "p" not in p
    assert p["t"] == hashlib.md5(("pw" + p["s"]).encode()).hexdigest()
    assert not start_scan(
        "https://m", "u", "p", get=lambda u, q: {"subsonic-response": {"status": "failed"}}
    )
