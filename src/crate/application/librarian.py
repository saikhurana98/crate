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

import contextlib
import dataclasses
import os
import shutil
from collections.abc import Callable
from pathlib import Path

from crate.application.download import DownloadReport
from crate.domain.library import TrackTags, library_relpath

TagReader = Callable[[Path], TrackTags]
TagWriter = Callable[[Path, TrackTags], None]
ArtFetcher = Callable[[str], bytes]  # art_url -> jpeg bytes (may raise)
CoverEmbedder = Callable[[Path, bytes], None]  # (audio_path, jpeg) -> embed front cover
Finalizer = Callable[[DownloadReport], DownloadReport]


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip()
    return text or None


def _merge(source: TrackTags, file: TrackTags, fallback_title: str) -> TrackTags:
    """Combine what the source told us with the file's own tags. The source wins
    field-by-field (it's authoritative — the file is often untagged); the file
    fills any gaps; title falls back to the request query so a placement never
    collapses to one ``Untitled`` file."""

    def pick(a: str | None, b: str | None) -> str | None:
        return _clean(a) or _clean(b)

    title = pick(source.title, file.title) or (fallback_title.strip() or "Untitled")
    return TrackTags(
        artist=pick(source.artist, file.artist),
        album=pick(source.album, file.album),
        title=title,
        album_artist=pick(source.album_artist, file.album_artist),
        track_no=source.track_no or file.track_no,
        disc_no=source.disc_no or file.disc_no,
    )


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


def _apply_art(
    produced: Path,
    art_url: str,
    fetch_art: ArtFetcher,
    embed_cover: CoverEmbedder,
) -> bytes | None:
    """Best-effort fetch of ``art_url`` + embed as ``produced``'s front cover.

    Returns the fetched JPEG bytes so the caller can also drop a ``cover.jpg``
    beside the final file. A failed/oversized fetch or an embed error must NEVER
    fail the download, so every exception is swallowed and ``None`` returned."""
    try:
        jpeg = fetch_art(art_url)
    except Exception:  # network / oversize / anything — art is optional
        return None
    with contextlib.suppress(Exception):  # container w/o picture support, corrupt file, …
        embed_cover(produced, jpeg)
    return jpeg


def finalize(
    produced: Path,
    root: Path,
    *,
    read_tags: TagReader,
    write_tags: TagWriter,
    source: TrackTags | None = None,
    fallback_title: str = "",
    art_url: str | None = None,
    fetch_art: ArtFetcher | None = None,
    embed_cover: CoverEmbedder | None = None,
) -> Path:
    """Tag + organize + move ``produced`` into the library; return its new path.

    ``source`` is the metadata the download source already knew (authoritative);
    it is merged over the file's own tags, and the result is written back so the
    library file is properly tagged for Navidrome.

    When ``art_url`` is set and both ``fetch_art`` + ``embed_cover`` are given,
    the album cover is best-effort fetched, embedded as a front cover, and also
    written as ``cover.jpg`` in the destination album directory (the beets /
    Navidrome layout). Art failures are swallowed — the file is still tagged and
    filed."""
    tags = _merge(source or TrackTags(), read_tags(produced), fallback_title or produced.stem)
    write_tags(produced, tags)
    jpeg: bytes | None = None
    if art_url and fetch_art is not None and embed_cover is not None:
        jpeg = _apply_art(produced, art_url, fetch_art, embed_cover)
    dest = _dedupe(root / library_relpath(tags, produced.suffix), produced)
    _atomic_move(produced, dest)
    if jpeg is not None:
        # album-folder cover.jpg — best-effort, never fatal
        with contextlib.suppress(OSError):
            (dest.parent / "cover.jpg").write_bytes(jpeg)
    return dest


def make_finalizer(
    root: Path,
    *,
    read_tags: TagReader,
    write_tags: TagWriter,
    fetch_art: ArtFetcher | None = None,
    embed_cover: CoverEmbedder | None = None,
) -> Finalizer:
    """A ``DownloadReport -> DownloadReport`` that finalizes the winner's file
    into ``root`` and rewrites the report's path so the recorder stores the
    library location. Reports without a produced file pass through untouched.

    ``fetch_art`` + ``embed_cover`` (when both provided) enable cover-art
    embedding using the outcome's ``art_url``; leaving either ``None`` keeps the
    pure tag+move behaviour."""

    def run(report: DownloadReport) -> DownloadReport:
        outcome = report.outcome
        if outcome is None:
            return report
        source = TrackTags(
            artist=outcome.artist,
            album=outcome.album,
            title=outcome.title,
            track_no=outcome.track_no,
            disc_no=outcome.disc_no,
        )
        final = finalize(
            outcome.path,
            root,
            read_tags=read_tags,
            write_tags=write_tags,
            source=source,
            fallback_title=report.query,
            art_url=outcome.art_url,
            fetch_art=fetch_art,
            embed_cover=embed_cover,
        )
        return dataclasses.replace(report, outcome=dataclasses.replace(outcome, path=final))

    return run
