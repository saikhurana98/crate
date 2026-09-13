"""Walk the library root for audio files (the dedup scan's disk side).

Iterative ``os.scandir`` (no recursion limit, one stat per file). Dot-entries are
skipped: the staging dir ``.lbmd-incoming``, the trash, and other tools' dirs.
Files modified within ``min_age_s`` are skipped because they may still be
written.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Iterator
from pathlib import Path

from crate.domain.library_scan import DiskEntry, is_audio


def walk_library(
    root: Path, *, min_age_s: float = 120.0, now: Callable[[], float] = time.time
) -> Iterator[DiskEntry]:
    cutoff_ns = int((now() - min_age_s) * 1_000_000_000)
    stack = [str(root)]
    base = len(str(root).rstrip(os.sep)) + 1
    while stack:
        current = stack.pop()
        try:
            entries = os.scandir(current)
        except OSError:
            continue  # unreadable dir: skip it, never abort the walk
        with entries:
            for entry in entries:
                if entry.name.startswith("."):
                    continue
                try:
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(entry.path)
                        continue
                    if not entry.is_file(follow_symlinks=False) or not is_audio(entry.name):
                        continue
                    st = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                if st.st_mtime_ns > cutoff_ns:
                    continue
                rel = entry.path[base:].replace(os.sep, "/")
                yield DiskEntry(rel, st.st_size, st.st_mtime_ns, st.st_ino or None)
