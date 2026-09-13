"""Tag read/write via mutagen — the infrastructure side of the librarian.

Uses mutagen's *easy* interface, which normalises FLAC / MP3 / MP4 / Ogg to a
common lowercase key set (``artist``, ``album``, ``title``, ``albumartist``,
``tracknumber``, ``discnumber``), so one code path covers every source's output.
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

import mutagen
from mutagen.flac import FLAC, Picture
from mutagen.id3 import APIC, ID3
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4, MP4Cover
from mutagen.oggopus import OggOpus
from mutagen.oggvorbis import OggVorbis

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


def has_cover(path: Path) -> bool:  # pragma: no cover - thin mutagen wrapper
    """True when the file carries embedded artwork (FLAC picture, ID3 APIC, MP4
    covr, or an Ogg METADATA_BLOCK_PICTURE)."""
    try:
        audio = mutagen.File(path)
    except Exception:
        audio = None
    if audio is None:
        return False
    if isinstance(audio, FLAC):
        return bool(audio.pictures)
    return _tags_have_cover(audio)


def _tags_have_cover(audio: Any) -> bool:  # pragma: no cover - thin mutagen wrapper
    tags = audio.tags
    if tags is None:
        return False
    if isinstance(audio, MP4):
        return bool(tags.get("covr"))
    if isinstance(tags, ID3):
        return bool(tags.getall("APIC"))
    return bool(tags.get("metadata_block_picture")) if hasattr(tags, "get") else False


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


_FRONT_COVER = 3  # ID3/FLAC picture type "Cover (front)"


def embed_cover(path: Path, jpeg: bytes) -> None:
    """Embed ``jpeg`` as the front cover of the audio file at ``path``.

    Container-agnostic: dispatches on the concrete mutagen type (FLAC / MP3 /
    MP4 / Ogg-Vorbis / Opus). Existing pictures are cleared first so a track
    carries exactly one front cover. Defensive — an unknown/unsupported
    container is a no-op rather than an error (the caller treats art as
    best-effort and must never fail a download over it)."""

    def _picture() -> Picture:  # FLAC / Ogg METADATA_BLOCK_PICTURE
        pic = Picture()
        pic.type = _FRONT_COVER
        pic.mime = "image/jpeg"
        pic.data = jpeg
        return pic

    audio = mutagen.File(path)
    if audio is None:  # pragma: no cover - unknown container, nothing to embed
        return
    if isinstance(audio, FLAC):
        audio.clear_pictures()
        audio.add_picture(_picture())
        audio.save()
    elif isinstance(audio, MP3 | ID3):  # pragma: no cover - id3 branch (mp3 fixture heavy)
        tags = audio if isinstance(audio, ID3) else audio.tags or ID3()
        tags.delall("APIC")
        tags.add(APIC(encoding=0, mime="image/jpeg", type=_FRONT_COVER, desc="", data=jpeg))
        tags.save(path)
    elif isinstance(audio, MP4):  # pragma: no cover - mp4 branch
        audio["covr"] = [MP4Cover(jpeg, imageformat=MP4Cover.FORMAT_JPEG)]
        audio.save()
    elif isinstance(audio, OggOpus | OggVorbis):  # pragma: no cover - ogg branch
        encoded = base64.b64encode(_picture().write()).decode("ascii")
        audio["metadata_block_picture"] = [encoded]
        audio.save()
