"""Librarian — finalize one finished download into the music library.

Runs per track the instant the cascade produces a verified file (both the
console-bulk queue worker and the Explo shim go through it), so tracks appear in
the library as they complete instead of after a whole batch. Steps:

  1. read the file's tags,
  2. fill missing artist/title from the request query, and write them back
     (organize *and* tag),
  3. atomically move the file to ``<root>/<Artist>/<Album>/<NN Title>.<ext>``.

Tag read/write are injected (mutagen lives in infrastructure) so this is unit
tested without real audio files.
"""

from __future__ import annotations

import dataclasses
import os
import shutil
from collections.abc import Callable
from pathlib import Path

from crate.application.download import DownloadReport
from crate.domain.library import TrackTags, library_relpath

TagReader = Callable[[Path], TrackTags]
TagWriter = Callable[[Path, TrackTags], None]
Finalizer = Callable[[DownloadReport], DownloadReport]


def _fill(tags: TrackTags, fallback_title: str) -> TrackTags:
    """Ensure at least a title so a placement never collapses to one file.
    Artist/album stay ``None`` (→ ``Unknown …``) when the source didn't tag."""
    if tags.title and tags.title.strip():
        return tags
    return dataclasses.replace(tags, title=fallback_title.strip() or "Untitled")


def _dedupe(dest: Path, source: Path) -> Path:
    """Avoid clobbering a different existing file: append `` (2)``, `` (3)`` … .
    If the target already *is* the same file (same size), reuse it."""
    if not dest.exists():
        return dest
    if dest.stat().st_size == source.stat().st_size:
        return dest  # already there (e.g. a re-run) — let the move overwrite in place
    stem, suffix = dest.stem, dest.suffix
    n = 2
    while True:
        cand = dest.with_name(f"{stem} ({n}){suffix}")
        if not cand.exists():
            return cand
        n += 1


def _atomic_move(source: Path, dest: Path) -> None:
    """Move ``source`` to ``dest`` atomically when on the same mount (rename),
    falling back to copy+replace across mounts. Parent dirs are created."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.parent / f".{dest.name}.part"
    shutil.move(str(source), str(tmp))  # copy+unlink across mounts, rename on same
    os.replace(tmp, dest)  # atomic within the destination directory


def finalize(
    produced: Path,
    root: Path,
    *,
    read_tags: TagReader,
    write_tags: TagWriter,
    fallback_title: str = "",
) -> Path:
    """Tag + organize + move ``produced`` into the library; return its new path."""
    tags = _fill(read_tags(produced), fallback_title or produced.stem)
    write_tags(produced, tags)
    dest = _dedupe(root / library_relpath(tags, produced.suffix), produced)
    _atomic_move(produced, dest)
    return dest


def make_finalizer(root: Path, *, read_tags: TagReader, write_tags: TagWriter) -> Finalizer:
    """A ``DownloadReport -> DownloadReport`` that finalizes the winner's file
    into ``root`` and rewrites the report's path so the recorder stores the
    library location. Reports without a produced file pass through untouched."""

    def run(report: DownloadReport) -> DownloadReport:
        if report.outcome is None:
            return report
        final = finalize(
            report.outcome.path,
            root,
            read_tags=read_tags,
            write_tags=write_tags,
            fallback_title=report.query,
        )
        return dataclasses.replace(report, outcome=dataclasses.replace(report.outcome, path=final))

    return run
