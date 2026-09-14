"""run_grouping end to end over real SQLite stores (library index + dedup tables)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from crate.application.identify import AcoustidMatch
from crate.application.jobs import JobContext
from crate.application.library_dedup import run_grouping
from crate.application.library_scan import FileAnalysis
from crate.domain.fingerprint import similarity
from crate.domain.jobs import JobKind
from crate.domain.library import TrackTags
from crate.domain.library_scan import DiskEntry, ScanPlan
from crate.infrastructure.sqlite.db import connect, now_iso
from crate.infrastructure.sqlite.dedup_store import SqliteDedupStore
from crate.infrastructure.sqlite.job_store import SqliteJobStore
from crate.infrastructure.sqlite.library_store import SqliteLibraryStore
from crate.infrastructure.sqlite.store import RequestStore

GOLDEN = json.loads(
    (Path(__file__).parents[1] / "domain" / "fixtures" / "chromaprint_golden.json").read_text()
)
RAW: list[int] = GOLDEN["sweep20"]["raw"]
NEAR = [x ^ (1 << (i % 32)) if i % 3 == 0 else x for i, x in enumerate(RAW)]  # ~1% bits flipped
OTHER = [x ^ 0xFFFF0000 for x in RAW]  # half the bits differ: a different recording
ROOT = Path("/library")


class World:
    def __init__(self, tmp_path: Path) -> None:
        self.conn = connect(tmp_path / "crate.db")
        self.lib = SqliteLibraryStore(self.conn)
        self.dedup = SqliteDedupStore(self.conn)
        self.jobs = SqliteJobStore(self.conn)
        self.jobs.enqueue(JobKind.DEDUP_SCAN, triggered_by="manual")
        job = self.jobs.claim_next("scan")
        assert job is not None
        self.ctx = JobContext(job, self.jobs)
        self.hashes: dict[str, str] = {}
        self.sim_calls = 0
        self.tick = 0

    def add(
        self,
        path: str,
        *,
        raw: list[int],
        size: int = 1000,
        lossless: bool = True,
        duration: float = 200.0,
        aid: str | None = None,
        tags: TrackTags | None = None,
        digest: str | None = None,
    ) -> str:
        self.lib.apply_plan(ScanPlan(new=[DiskEntry(path, size, 1, None)]))
        fid = next(f.id for f in self.lib.indexed() if f.path == path)
        self.lib.save_analysis(
            fid,
            FileAnalysis(
                codec="flac" if lossless else "mp3",
                container="flac" if lossless else "mp3",
                lossless=lossless,
                bitrate=None if lossless else 320_000,
                bit_depth=16 if lossless else None,
                sample_rate=44100,
                channels=2,
                duration_s=duration,
                tags=tags or TrackTags(artist="A", title="Song"),
                has_cover=True,
                fp_duration=int(duration),
                fp_compressed="x",
                fp_raw=raw,
            ),
        )
        self.lib.save_lookup(fid, AcoustidMatch(aid, 0.95, None, None, None) if aid else None)
        self.hashes[str(ROOT / path)] = digest or f"h-{path}"
        return fid

    def sim(self, a: Any, b: Any, **kw: Any) -> float:
        self.sim_calls += 1
        return similarity(a, b, **kw)

    def run(self) -> dict[str, Any]:
        self.tick += 1
        return run_grouping(
            self.ctx,
            self.dedup,
            root=ROOT,
            hash_file=lambda p: self.hashes[str(p)],
            now=now_iso(),
            sim=self.sim,
        )


def test_groups_from_acoustid_fp_and_exact_evidence(tmp_path: Path) -> None:
    w = World(tmp_path)
    keep = w.add(
        "A/LP/01 Song.flac",
        raw=RAW,
        aid="aid-1",
        tags=TrackTags(artist="A", album="LP", title="Song", track_no=1),
    )
    mp3 = w.add("dupes/Song.mp3", raw=RAW, lossless=False, aid="aid-1")
    near = w.add("misc/song copy.mp3", raw=NEAR, lossless=False, duration=201.0)
    w.add("other/Different.flac", raw=OTHER, duration=201.5)
    a = w.add("x/a.flac", raw=[], size=999, digest="same")
    b = w.add("x/b.flac", raw=[], size=999, digest="same")
    w.add("x/c.flac", raw=[], size=999, digest="other")
    s = w.run()
    assert s["new_edges"]["exact"] == 1 and s["new_edges"]["acoustid"] == 1
    assert s["new_edges"]["fp"] >= 1 and s["groups"] == 2
    groups = {g["reason"]: g for g in w.dedup.groups()}
    song = groups["acoustid"]
    assert song["keeper_file_id"] == keep and not song["variant"]
    members = {m["id"]: m for m in song["members"]}
    assert set(members) == {keep, mp3, near}
    assert members[keep]["is_keeper"] and members[keep]["canonical"] is True
    assert members[mp3]["reasons"] == ["lossless beats lossy"]
    assert members[mp3]["score_vs_keeper"] == 1.0 and members[near]["score_vs_keeper"] > 0.95
    assert song["reclaimable_bytes"] == 2000
    assert {m["id"] for m in groups["exact"]["members"]} == {a, b}
    ov = w.dedup.overview()
    assert (ov["groups"], ov["duplicate_files"], ov["reclaimable_bytes"]) == (2, 3, 2999)


def test_rerun_with_nothing_new_does_no_comparisons_and_keeps_ids(tmp_path: Path) -> None:
    w = World(tmp_path)
    w.add("a.flac", raw=RAW, aid="aid-1")
    w.add("b.mp3", raw=RAW, lossless=False, aid="aid-1")
    w.run()
    before = [g["id"] for g in w.dedup.groups()]
    w.sim_calls = 0
    s = w.run()
    assert s["compared"] == 0 and w.sim_calls == 1  # only the keeper-similarity for display
    assert [g["id"] for g in w.dedup.groups()] == before


def test_new_file_joins_an_existing_group(tmp_path: Path) -> None:
    w = World(tmp_path)
    w.add("a.flac", raw=RAW, aid="aid-1")
    w.add("b.mp3", raw=RAW, lossless=False, aid="aid-1")
    w.run()
    w.add("c.mp3", raw=RAW, lossless=False, aid="aid-1")
    s = w.run()
    assert s["compared"] == 1 and len(w.dedup.groups()[0]["members"]) == 3


def test_ignored_groups_stay_gone_and_missing_files_drop_out(tmp_path: Path) -> None:
    w = World(tmp_path)
    a = w.add("a.flac", raw=RAW, aid="aid-1")
    w.add("b.mp3", raw=RAW, lossless=False, aid="aid-1")
    w.run()
    gid = w.dedup.groups()[0]["id"]
    assert w.dedup.ignore_group(gid, "u1") == 1
    assert w.dedup.ignore_group("nope", "u1") is None
    w.add("c.mp3", raw=RAW, lossless=False, aid="aid-1")
    w.run()
    group = w.dedup.groups()[0]
    assert len(group["members"]) == 3 and group["id"] != gid  # c links to both; a-b stays ignored

    w.lib.apply_plan(ScanPlan(missing=[a]))
    w.run()
    assert [len(g["members"]) for g in w.dedup.groups()] == [2]


def test_long_length_spread_is_a_variant_with_nothing_to_reclaim(tmp_path: Path) -> None:
    w = World(tmp_path)
    w.add("album.flac", raw=RAW, aid="aid-1", duration=240.0)
    w.add("radio edit.flac", raw=RAW, aid="aid-1", duration=205.0)
    w.run()
    group = w.dedup.groups()[0]
    assert group["variant"] is True and group["reclaimable_bytes"] == 0
    assert w.dedup.groups(variants=False) == []


def test_download_records_mark_files_referenced(tmp_path: Path) -> None:
    w = World(tmp_path)
    w.add("a.flac", raw=RAW, aid="aid-1")
    mp3 = w.add("b.mp3", raw=RAW, lossless=False, aid="aid-1")
    requests = RequestStore(w.conn)
    rid = requests.enqueue("B")
    requests.complete_as_duplicate(
        rid,
        {
            "path": str(ROOT / "b.mp3"),
            "size_bytes": 1000,
            "codec": "mp3",
            "bitrate": 320_000,
            "bit_depth": None,
            "sample_rate": 44100,
            "duration_s": 200.0,
            "lossless": 0,
            "quality_label": "lossy mp3 320 kbps",
        },
    )
    w.run()
    members = {m["id"]: m for m in w.dedup.groups()[0]["members"]}
    assert members[mp3]["referenced"] is True
