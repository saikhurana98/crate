"""Purge rules (pure): when a duplicate copy may be trashed, and where it goes.

A copy is refused, and stays exactly where it is, when any of these hold:

* it's the copy being kept;
* it or the keeper is gone from the index or the disk;
* its size or mtime changed since the user reviewed it;
* a queued or running download is about to replace it.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from pathlib import PurePosixPath
from typing import Any

TRASH_DIR = ".crate-trash"  # a dot dir: the scan walker and Navidrome both skip it


@dataclass(frozen=True)
class PurgeItem:
    """One copy to trash, with what the user saw when they chose it."""

    file_id: str
    keeper_id: str
    group_id: str | None
    size: int
    mtime_ns: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_dict(raw: Mapping[str, Any]) -> PurgeItem:
        return PurgeItem(
            file_id=str(raw["file_id"]),
            keeper_id=str(raw["keeper_id"]),
            group_id=str(raw["group_id"]) if raw.get("group_id") else None,
            size=int(raw["size"]),
            mtime_ns=int(raw["mtime_ns"]),
        )


def trash_relpath(rel: str, day: str) -> str:
    """Where a file goes in the trash: its original path under a per-day folder."""
    return f"{TRASH_DIR}/{day}/{rel}"


def restored_name(rel: str, n: int) -> str:
    p = PurePosixPath(rel)
    return str(p.with_name(f"{p.stem} (restored-{n}){p.suffix}"))


def free_path(rel: str, taken: Callable[[str], bool]) -> str:
    """``rel``, or ``<stem> (restored-N)<ext>`` when something already has it."""
    if not taken(rel):
        return rel
    n = 1
    while taken(candidate := restored_name(rel, n)):
        n += 1
    return candidate


def refuse_reason(
    item: PurgeItem,
    *,
    file_row: Mapping[str, Any] | None,
    keeper_row: Mapping[str, Any] | None,
    on_disk: tuple[int, int] | None,
    keeper_on_disk: bool,
    inflight: bool,
) -> str | None:
    """Why this copy must not be trashed right now, or None if it may be. The first
    failing check (in this order) is the reason given."""
    checks = (
        (item.file_id == item.keeper_id, "it's the copy being kept"),
        (file_row is None, "it's no longer in the library index"),
        (keeper_row is None or not keeper_on_disk, "the copy to keep is gone"),
        (on_disk is None, "the file is already gone"),
        (on_disk != (item.size, item.mtime_ns), "the file changed since you reviewed it"),
        (inflight, "a download is about to replace it"),
    )
    return next((reason for failed, reason in checks if failed), None)
