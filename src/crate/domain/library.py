"""Library placement — pure rules for where a finished track lives on disk.

Given a track's tags, decide its path *relative to the library root*, as
``<AlbumArtist>/<Album>/<NN Title>.<ext>``. No I/O: reading the tags and moving
the file are the librarian's job (application + infrastructure). Kept pure so the
naming/sanitising rules are exhaustively unit-tested.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

# Characters illegal on common filesystems (and path separators) → underscore.
_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_MAX = 180  # keep each path component well under filesystem limits


@dataclass(frozen=True, slots=True)
class TrackTags:
    """The subset of metadata used to file a track. All optional — missing
    values fall back to ``Unknown …`` so a placement always resolves."""

    artist: str | None = None
    album: str | None = None
    title: str | None = None
    album_artist: str | None = None
    track_no: int | None = None
    disc_no: int | None = None


def sanitize(name: str) -> str:
    """Make ``name`` safe as a single path component: replace illegal chars,
    collapse whitespace, drop trailing dots/spaces (Windows/SMB hostile), cap
    length. Never returns empty."""
    cleaned = _UNSAFE.sub("_", name)
    cleaned = re.sub(r"\s+", " ", cleaned).strip().rstrip(".").strip()
    return cleaned[:_MAX] or "_"


def library_relpath(tags: TrackTags, ext: str) -> Path:
    """Relative destination path for a track under the library root.

    ``<AlbumArtist or Artist>/<Album>/<NN Title>.<ext>`` — the track number is
    zero-padded and prefixed only when present; a disc number > 1 prefixes it as
    ``D-NN`` so multi-disc albums stay ordered. ``ext`` is the file suffix,
    including the leading dot (e.g. ``.flac``).
    """
    artist = sanitize(tags.album_artist or tags.artist or "Unknown Artist")
    album = sanitize(tags.album or "Unknown Album")
    title = sanitize(tags.title or "Untitled")
    prefix = ""
    if tags.track_no:
        disc = f"{tags.disc_no}-" if tags.disc_no and tags.disc_no > 1 else ""
        prefix = f"{disc}{tags.track_no:02d} "
    suffix = ext if ext.startswith(".") else f".{ext}"
    return Path(artist) / album / f"{prefix}{title}{suffix}"
