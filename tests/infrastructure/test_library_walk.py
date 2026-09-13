"""walk_library: audio only, dot-entries skipped, fresh files skipped, relative paths."""

from __future__ import annotations

import os
from pathlib import Path

from crate.infrastructure.fs.library_walk import walk_library


def _touch(path: Path, data: bytes = b"x", age_s: float = 3600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    t = path.stat().st_mtime - age_s
    os.utime(path, (t, t))


def test_walk_finds_audio_and_skips_the_rest(tmp_path: Path) -> None:
    _touch(tmp_path / "Artist" / "Album" / "01 Song.flac", b"abcd")
    _touch(tmp_path / "Artist" / "Album" / "cover.jpg")
    _touch(tmp_path / "Artist" / "Single.MP3")
    _touch(tmp_path / ".lbmd-incoming" / "partial.flac")  # staging dir
    _touch(tmp_path / "Artist" / ".hidden.flac")
    _touch(tmp_path / "Artist" / "Album" / "02 Fresh.flac", age_s=0)  # still being written
    entries = sorted(walk_library(tmp_path), key=lambda e: e.path)
    assert [e.path for e in entries] == ["Artist/Album/01 Song.flac", "Artist/Single.MP3"]
    song = entries[0]
    assert song.size == 4 and song.inode and song.mtime_ns > 0


def test_min_age_is_configurable(tmp_path: Path) -> None:
    _touch(tmp_path / "a.flac", age_s=0)
    assert [e.path for e in walk_library(tmp_path, min_age_s=0, now=lambda: 4e9)] == ["a.flac"]


def test_unreadable_or_missing_dirs_do_not_abort(tmp_path: Path) -> None:
    assert list(walk_library(tmp_path / "nope")) == []
