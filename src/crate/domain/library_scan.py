"""Incremental library-scan planning (pure): diff what's on disk against the index.

The walker reports every audio file as a :class:`DiskEntry`. :func:`plan_scan`
sorts each one into new, changed (size or mtime moved, so re-analyse), moved (a
vanished file's inode + size reappeared at another path, so it keeps its id),
unchanged, and missing.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

AUDIO_EXTENSIONS = frozenset(
    {
        ".flac",
        ".mp3",
        ".m4a",
        ".aac",
        ".alac",
        ".ogg",
        ".oga",
        ".opus",
        ".wav",
        ".aif",
        ".aiff",
        ".wma",
        ".ape",
        ".wv",
    }
)


def is_audio(name: str) -> bool:
    dot = name.rfind(".")
    return dot > 0 and name[dot:].lower() in AUDIO_EXTENSIONS


@dataclass(frozen=True)
class DiskEntry:
    path: str  # relative to the library root, POSIX separators
    size: int
    mtime_ns: int
    inode: int | None


@dataclass(frozen=True)
class IndexedFile:
    id: str
    path: str
    size: int
    mtime_ns: int
    inode: int | None
    missing: bool


@dataclass
class ScanPlan:
    new: list[DiskEntry] = field(default_factory=list)
    changed: list[tuple[str, DiskEntry]] = field(default_factory=list)  # re-analyse
    moved: list[tuple[str, DiskEntry]] = field(default_factory=list)  # same file, new path
    seen: list[tuple[str, DiskEntry]] = field(default_factory=list)  # unchanged
    missing: list[str] = field(default_factory=list)  # ids newly gone from disk


def plan_scan(indexed: Iterable[IndexedFile], disk: Iterable[DiskEntry]) -> ScanPlan:
    plan = ScanPlan()
    by_path = {f.path: f for f in indexed}
    present: set[str] = set()
    unmatched: list[DiskEntry] = []
    for entry in disk:
        known = by_path.get(entry.path)
        if known is None:
            unmatched.append(entry)
            continue
        present.add(entry.path)
        same = known.size == entry.size and known.mtime_ns == entry.mtime_ns
        (plan.seen if same else plan.changed).append((known.id, entry))
    gone = [f for path, f in by_path.items() if path not in present]
    by_inode: dict[tuple[int, int], IndexedFile] = {}
    for f in gone:
        if f.inode is not None:
            by_inode.setdefault((f.inode, f.size), f)
    moved: set[str] = set()
    for entry in unmatched:
        twin = by_inode.pop((entry.inode, entry.size), None) if entry.inode is not None else None
        if twin is None:
            plan.new.append(entry)
        else:
            plan.moved.append((twin.id, entry))
            moved.add(twin.id)
    plan.missing = [f.id for f in gone if f.id not in moved and not f.missing]
    return plan
