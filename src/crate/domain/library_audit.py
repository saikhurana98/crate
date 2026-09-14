"""Library audits (pure): the Dedup page's "anything else that helps" reports.

* **Tag audit:** a file whose tags disagree with what AcoustID says the recording
  is. This usually means the wrong song was grabbed, or bad tags that hide a
  duplicate.
* **Album-folder duplicates:** a folder whose tracks are mostly duplicated by
  the tracks of one other folder, i.e. the same album filed twice (a
  re-download, a "Deluxe" copy, a rip under a different artist spelling).
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from crate.domain.matching import artist_matches, title_matches


def tags_disagree(
    tag_artist: str | None,
    tag_title: str | None,
    aid_artist: str | None,
    aid_title: str | None,
) -> str | None:
    """Which tagged field disagrees with AcoustID's name for the recording:
    ``"title"``, ``"artist"``, ``"both"``, or ``None`` when they agree (or AcoustID
    has no name). A tag that only *adds* words ("Lover (Official Video)" for
    AcoustID's "Lover") still agrees."""
    title_bad = bool(aid_title) and not title_matches(str(aid_title), tag_title or "")
    artist_bad = bool(aid_artist) and not artist_matches(tag_artist or "", str(aid_artist))
    if title_bad and artist_bad:
        return "both"
    if title_bad:
        return "title"
    return "artist" if artist_bad else None


@dataclass(frozen=True)
class FolderDup:
    folder: str
    other: str
    shared: int  # duplicate groups with a copy in both folders
    files: int  # tracks in ``folder``
    other_files: int
    ratio: float  # shared / files


def folder_of(path: str) -> str:
    return path.rsplit("/", 1)[0] if "/" in path else ""


def folder_duplicates(
    members: Iterable[tuple[str, str]],
    folder_sizes: Mapping[str, int],
    *,
    threshold: float = 0.7,
    min_shared: int = 2,
) -> list[FolderDup]:
    """``members`` is ``(group_id, folder)`` for every duplicate-group member. A
    folder qualifies when at least ``threshold`` of its tracks have a duplicate in
    one other folder, and at least ``min_shared`` tracks are shared. Each pair is
    reported once, from the side that qualifies (or the smaller folder if both do)."""
    by_group: dict[str, set[str]] = {}
    for group_id, folder in members:
        by_group.setdefault(group_id, set()).add(folder)
    shared: Counter[tuple[str, str]] = Counter()
    for folders in by_group.values():
        for a in folders:
            for b in folders:
                if a != b:
                    shared[(a, b)] += 1
    found: dict[frozenset[str], FolderDup] = {}
    for (a, b), n in shared.items():
        files = folder_sizes.get(a, 0)
        if n < min_shared or not files or n / files < threshold:
            continue
        dup = FolderDup(a, b, n, files, folder_sizes.get(b, 0), round(n / files, 3))
        key = frozenset((a, b))
        best = found.get(key)
        if best is None or (dup.files, dup.folder) < (best.files, best.folder):
            found[key] = dup
    return sorted(found.values(), key=lambda d: (-d.shared, d.folder, d.other))
