"""Deduplicate: find duplicate groups in the library index and rank each group's copies.

Runs as the last stage of the ``dedup_scan`` job. Only files that are new or
changed since the last grouping are compared again; stored edges between
unchanged files are reused. Evidence:

* ``exact``: same size, then the same sha256. Files are hashed only when their
  sizes collide.
* ``acoustid``: same AcoustID track id, both scores at least 0.8, and the
  fingerprints at least 0.90 similar.
* ``fp``: for files AcoustID couldn't identify, any library file within 3 s of
  its length that passes a cheap 64-frame prefilter and then scores at least
  0.92 similar. Two identified files are left to AcoustID; comparing every such
  pair would be too slow in Python.

Groups are rebuilt from all edges each run, minus ignored pairs, and ranked.
"""

from __future__ import annotations

import bisect
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from crate.application.jobs import JobContext
from crate.domain.fingerprint import similarity
from crate.domain.library import TrackTags, library_relpath
from crate.domain.library_dedup import (
    ACOUSTID,
    EXACT,
    FP,
    Component,
    Edge,
    FileFacts,
    connected_groups,
    is_variant,
    pair,
    rank_files,
)

ACOUSTID_MIN_SCORE = 0.8
ACOUSTID_CONFIRM = 0.90
FP_THRESHOLD = 0.92
FP_PREFILTER = 0.75
FP_WINDOW_S = 3.0
PREFILTER_FRAMES = 64
COMPARE_FRAMES = 500  # ~60 s of audio: plenty to tell copies apart, and bounded cost
MAX_OFFSET = 12  # copies of one recording line up within a frame or two

Similarity = Callable[..., float]
Hasher = Callable[[Path], str]


@dataclass(frozen=True)
class IndexRow:
    id: str
    path: str
    size: int
    duration_s: float | None
    acoustid_id: str | None
    acoustid_score: float | None
    content_hash: str | None
    changed_at: str | None  # latest of analysed / looked up
    raw: list[int]


@dataclass(frozen=True)
class MemberRecord:
    file_id: str
    rank: int
    score_vs_keeper: float | None
    reasons: list[str]
    facts: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class GroupRecord:
    id: str
    reason: str
    confidence: float
    variant: bool
    keeper_file_id: str
    reclaimable_bytes: int
    members: list[MemberRecord]


class DedupStore(Protocol):
    def live_rows(self) -> list[IndexRow]: ...
    def last_grouped_at(self) -> str | None: ...
    def set_last_grouped_at(self, ts: str) -> None: ...
    def set_hash(self, file_id: str, digest: str) -> None: ...
    def replace_edges(self, dirty: set[str], edges: list[Edge]) -> None: ...
    def all_edges(self) -> list[Edge]: ...
    def ignored_pairs(self) -> set[tuple[str, str]]: ...
    def facts(self, ids: Iterable[str]) -> dict[str, dict[str, Any]]: ...
    def referenced_paths(self) -> set[str]: ...
    def replace_groups(self, groups: list[GroupRecord]) -> None: ...


def _exact_edges(
    rows: list[IndexRow], dirty: set[str], root: Path, hash_file: Hasher, store: DedupStore
) -> list[Edge]:
    by_size: dict[int, list[IndexRow]] = {}
    for r in rows:
        by_size.setdefault(r.size, []).append(r)
    edges: list[Edge] = []
    for same in by_size.values():
        if len(same) <= 1 or not any(r.id in dirty for r in same):
            continue
        by_digest: dict[str, list[str]] = {}
        for r in same:
            digest = r.content_hash
            if digest is None:
                try:
                    digest = hash_file(root / r.path)
                except OSError:
                    continue  # vanished since the walk
                store.set_hash(r.id, digest)
            by_digest.setdefault(digest, []).append(r.id)
        for ids in by_digest.values():
            for i, x in enumerate(ids):
                edges += [
                    Edge.of(x, y, EXACT, 1.0) for y in ids[i + 1 :] if x in dirty or y in dirty
                ]
    return edges


def _compare(sim: Similarity, a: list[int], b: list[int]) -> float:
    return float(sim(a[:COMPARE_FRAMES], b[:COMPARE_FRAMES], max_offset=MAX_OFFSET))


def _acoustid_edges(rows: list[IndexRow], dirty: set[str], sim: Similarity) -> list[Edge]:
    by_track: dict[str, list[IndexRow]] = {}
    for r in rows:
        if r.acoustid_id and (r.acoustid_score or 0) >= ACOUSTID_MIN_SCORE and r.raw:
            by_track.setdefault(r.acoustid_id, []).append(r)
    edges: list[Edge] = []
    for same in by_track.values():
        for i, x in enumerate(same):
            for y in same[i + 1 :]:
                if x.id not in dirty and y.id not in dirty:
                    continue
                score = _compare(sim, x.raw, y.raw)
                if score >= ACOUSTID_CONFIRM:
                    edges.append(Edge.of(x.id, y.id, ACOUSTID, round(score, 4)))
    return edges


def _fp_edges(
    ctx: JobContext,
    rows: list[IndexRow],
    dirty: set[str],
    linked: set[tuple[str, str]],
    sim: Similarity,
) -> list[Edge]:
    timed = sorted((r for r in rows if r.raw and r.duration_s), key=lambda r: r.duration_s or 0)
    lengths = [r.duration_s or 0.0 for r in timed]
    todo = [r for r in timed if r.id in dirty]
    edges: list[Edge] = []
    seen: set[tuple[str, str]] = set()
    for n, d in enumerate(todo, 1):
        if n % 200 == 0:
            ctx.check_cancelled()
            ctx.progress(n, len(todo), f"comparing fingerprints {n}/{len(todo)}")
        length = d.duration_s or 0.0
        lo = bisect.bisect_left(lengths, length - FP_WINDOW_S)
        hi = bisect.bisect_right(lengths, length + FP_WINDOW_S)
        for o in timed[lo:hi]:
            if o.id == d.id or (d.acoustid_id and o.acoustid_id):
                continue
            p = pair(d.id, o.id)
            if p in seen or p in linked:
                continue
            seen.add(p)
            head = sim(
                d.raw[:PREFILTER_FRAMES],
                o.raw[:PREFILTER_FRAMES],
                max_offset=5,
                min_overlap=PREFILTER_FRAMES // 2,
            )
            if head < FP_PREFILTER:
                continue
            score = _compare(sim, d.raw, o.raw)
            if score >= FP_THRESHOLD:
                edges.append(Edge.of(d.id, o.id, FP, round(score, 4)))
    return edges


def file_facts(row: dict[str, Any], root: Path, referenced: set[str]) -> FileFacts:
    tags = TrackTags(
        artist=row.get("artist"),
        album=row.get("album"),
        title=row.get("title"),
        album_artist=row.get("album_artist"),
        track_no=row.get("track_no"),
        disc_no=row.get("disc_no"),
    )
    path = str(row["path"])
    named = bool((tags.artist or tags.album_artist) and tags.title)
    canonical = named and library_relpath(tags, Path(path).suffix).as_posix() == path
    keys = ("artist", "title", "album", "album_artist", "track_no")
    return FileFacts(
        id=str(row["id"]),
        path=path,
        lossless=bool(row.get("lossless")),
        bit_depth=row.get("bit_depth"),
        sample_rate=row.get("sample_rate"),
        bitrate=row.get("bitrate"),
        duration_s=row.get("duration_s"),
        tag_score=sum(1 for k in keys if row.get(k)),
        has_cover=bool(row.get("has_cover")),
        canonical=canonical,
        referenced=str(root / path) in referenced,
    )


def _build_groups(
    comps: list[Component],
    rows: dict[str, IndexRow],
    store: DedupStore,
    root: Path,
    sim: Similarity,
) -> list[GroupRecord]:
    facts_rows = store.facts({m for c in comps for m in c.members})
    referenced = store.referenced_paths()
    out: list[GroupRecord] = []
    for c in comps:
        facts = [file_facts(facts_rows[m], root, referenced) for m in c.members if m in facts_rows]
        if len(facts) <= 1:
            continue
        ranked = rank_files(facts)
        keeper = ranked[0].facts
        variant = is_variant([f.duration_s for f in facts if f.duration_s])
        members = []
        for r in ranked:
            score = None
            k_raw, m_raw = rows.get(keeper.id), rows.get(r.facts.id)
            if r.rank and k_raw and m_raw and k_raw.raw and m_raw.raw:
                score = round(_compare(sim, k_raw.raw, m_raw.raw), 4)
            summary = {
                "tag_score": r.facts.tag_score,
                "canonical": r.facts.canonical,
                "referenced": r.facts.referenced,
            }
            members.append(MemberRecord(r.facts.id, r.rank, score, r.reasons, summary))
        reclaim = 0 if variant else sum(int(facts_rows[r.facts.id]["size"]) for r in ranked[1:])
        out.append(
            GroupRecord(c.signature, c.reason, c.confidence, variant, keeper.id, reclaim, members)
        )
    return out


def run_grouping(
    ctx: JobContext,
    store: DedupStore,
    *,
    root: Path,
    hash_file: Hasher,
    now: str,
    sim: Similarity = similarity,
) -> dict[str, Any]:
    """Recompute edges for new or changed files, rebuild and rank the groups."""
    rows = store.live_rows()
    last = store.last_grouped_at()
    dirty = {r.id for r in rows if last is None or (r.changed_at or "") > last}
    ctx.log(f"grouping: {len(dirty)} new or changed of {len(rows)} files", code="group_start")
    edges: list[Edge] = []
    if dirty:
        edges = _exact_edges(rows, dirty, root, hash_file, store)
        edges += _acoustid_edges(rows, dirty, sim)
        linked = {(e.a, e.b) for e in edges}
        edges += _fp_edges(ctx, rows, dirty, linked, sim)
    store.replace_edges(dirty, edges)  # also drops edges of files that left the index
    comps = connected_groups(store.all_edges(), store.ignored_pairs())
    groups = _build_groups(comps, {r.id: r for r in rows}, store, root, sim)
    store.replace_groups(groups)
    store.set_last_grouped_at(now)
    real = [g for g in groups if not g.variant]
    counts = {kind: sum(1 for e in edges if e.kind == kind) for kind in (EXACT, ACOUSTID, FP)}
    reclaimable = sum(g.reclaimable_bytes for g in real)
    summary: dict[str, Any] = {
        "compared": len(dirty),
        "new_edges": counts,
        "groups": len(groups),
        "variants": len(groups) - len(real),
        "duplicate_files": sum(len(g.members) - 1 for g in real),
        "reclaimable_bytes": reclaimable,
    }
    gb = reclaimable / 1e9
    ctx.log(
        f"{len(groups)} duplicate groups ({summary['variants']} variants); "
        f"{summary['duplicate_files']} extra copies, {gb:.1f} GB reclaimable",
        code="groups",
        data=summary,
    )
    return summary
