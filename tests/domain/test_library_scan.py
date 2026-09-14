"""plan_scan: new / changed / moved / unchanged / missing; is_audio."""

from __future__ import annotations

import pytest

from crate.domain.library_scan import DiskEntry, IndexedFile, is_audio, plan_scan


def disk(path: str, size: int = 100, mtime: int = 1, inode: int | None = None) -> DiskEntry:
    return DiskEntry(path, size, mtime, inode)


def known(
    fid: str,
    path: str,
    size: int = 100,
    mtime: int = 1,
    inode: int | None = None,
    missing: bool = False,
) -> IndexedFile:
    return IndexedFile(fid, path, size, mtime, inode, missing)


@pytest.mark.parametrize(
    ("name", "ok"),
    [
        ("a.flac", True),
        ("A.MP3", True),
        ("x.m4a", True),
        ("cover.jpg", False),
        ("flac", False),
        (".flac", False),
        ("Bakki.webm", True),
    ],
)
def test_is_audio(name: str, ok: bool) -> None:
    assert is_audio(name) is ok


def test_first_scan_is_all_new() -> None:
    plan = plan_scan([], [disk("a/1.flac"), disk("a/2.flac")])
    assert [e.path for e in plan.new] == ["a/1.flac", "a/2.flac"]
    assert not (plan.changed or plan.moved or plan.seen or plan.missing)


def test_unchanged_changed_and_missing() -> None:
    plan = plan_scan(
        [known("f1", "a/1.flac"), known("f2", "a/2.flac"), known("f3", "a/3.flac")],
        [disk("a/1.flac"), disk("a/2.flac", size=222)],
    )
    assert [fid for fid, _ in plan.seen] == ["f1"]
    assert [fid for fid, _ in plan.changed] == ["f2"]
    assert plan.missing == ["f3"] and plan.new == []


def test_a_move_keeps_its_id_by_inode_and_size() -> None:
    plan = plan_scan(
        [known("f1", "old/1.flac", inode=77), known("f2", "old/2.flac", inode=78)],
        [disk("new/1.flac", inode=77), disk("new/2.flac", size=5, inode=78)],
    )
    assert [(fid, e.path) for fid, e in plan.moved] == [("f1", "new/1.flac")]
    assert [e.path for e in plan.new] == ["new/2.flac"]  # size differs -> not the same file
    assert plan.missing == ["f2"]


def test_already_missing_files_are_not_re_reported_but_can_come_back() -> None:
    plan = plan_scan(
        [known("f1", "a/1.flac", missing=True), known("f2", "a/2.flac", missing=True)],
        [disk("a/1.flac")],
    )
    assert [fid for fid, _ in plan.seen] == ["f1"]  # back at the same path
    assert plan.missing == []  # f2 was already missing


def test_unknown_inodes_never_count_as_moves() -> None:
    plan = plan_scan([known("f1", "a/1.flac")], [disk("b/1.flac")])
    assert [e.path for e in plan.new] == ["b/1.flac"] and plan.missing == ["f1"]
