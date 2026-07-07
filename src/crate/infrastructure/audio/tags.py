"""Tag read/write via mutagen — the infrastructure side of the librarian.

Uses mutagen's *easy* interface, which normalises FLAC / MP3 / MP4 / Ogg to a
common lowercase key set (``artist``, ``album``, ``title``, ``albumartist``,
``tracknumber``, ``discnumber``), so one code path covers every source's output.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import mutagen

from crate.domain.library import TrackTags


def _first(tags: Any, key: str) -> str | None:
    value = tags.get(key)
    if not value:
        return None
    item = value[0] if isinstance(value, list) else value
    text = str(item).strip()
    return text or None


def _num(tags: Any, key: str) -> int | None:
    # tags like "3" or "3/12" (track-of-total) — take the leading integer.
    raw = _first(tags, key)
    if raw is None:
        return None
    head = raw.split("/", 1)[0].strip()
    try:
        return int(head)
    except ValueError:
        return None


def read_tags(path: Path) -> TrackTags:  # pragma: no cover - thin mutagen wrapper
    audio = mutagen.File(path, easy=True)
    if audio is None:
        return TrackTags()
    return TrackTags(
        artist=_first(audio, "artist"),
        album=_first(audio, "album"),
        title=_first(audio, "title"),
        album_artist=_first(audio, "albumartist"),
        track_no=_num(audio, "tracknumber"),
        disc_no=_num(audio, "discnumber"),
    )


def write_tags(path: Path, tags: TrackTags) -> None:  # pragma: no cover - thin mutagen wrapper
    audio = mutagen.File(path, easy=True)
    if audio is None:  # unknown container — nothing to write, leave the file alone
        return
    fields = {
        "artist": tags.artist,
        "album": tags.album,
        "title": tags.title,
        "albumartist": tags.album_artist,
        "tracknumber": str(tags.track_no) if tags.track_no else None,
        "discnumber": str(tags.disc_no) if tags.disc_no else None,
    }
    for key, value in fields.items():
        if value:
            audio[key] = value
    audio.save()
