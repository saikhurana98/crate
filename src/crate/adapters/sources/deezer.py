"""Tier-2 source adapter: Deezer -> 16-bit FLAC.

Deezer streams are Blowfish-CBC "stripe" encrypted with a per-track key derived
from the track id and a fixed secret — no DRM server, no browser, no CENC. Given
a valid ARL session (supplied by the user, handled by the injected client), this
is a clean headless lossless source.

Pure here: key derivation, the stripe decryption, search parsing, selection.
The HTTP flow (auth/search/token/get_url/download) is injected via callables.
"""

from __future__ import annotations

import hashlib
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from Crypto.Cipher import Blowfish

from crate.domain.download import DownloadOutcome
from crate.domain.matching import title_matches

_SECRET = b"g4el58wc0zvf9na1"  # Deezer's fixed key-derivation secret
_IV = bytes(range(8))  # 00 01 02 03 04 05 06 07
_CHUNK = 2048

Search = Callable[[str], Any]  # query -> Deezer search JSON
ResolveUrl = Callable[[int], str]  # track id -> encrypted CDN url (client does token+get_url)
GetBytes = Callable[[str], bytes]
Clock = Callable[[], float]

_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


@dataclass(frozen=True, slots=True)
class DeezerTrack:
    id: int
    title: str
    artist: str
    album: str
    duration_s: int | None
    isrc: str | None


def blowfish_key(track_id: int) -> bytes:
    """Per-track Blowfish key: XOR of the two halves of md5(track_id) with the secret."""
    md5 = hashlib.md5(str(track_id).encode()).hexdigest()
    return bytes(ord(md5[i]) ^ ord(md5[i + 16]) ^ _SECRET[i] for i in range(16))


def decrypt_track(data: bytes, key: bytes) -> bytes:
    """Reverse Deezer's CBC-stripe: every 3rd full 2048-byte chunk is Blowfish-CBC
    encrypted, the rest (and any trailing partial chunk) is clear."""
    out = bytearray()
    for index, start in enumerate(range(0, len(data), _CHUNK)):
        chunk = data[start : start + _CHUNK]
        if index % 3 == 0 and len(chunk) == _CHUNK:
            out += Blowfish.new(key, Blowfish.MODE_CBC, _IV).decrypt(chunk)
        else:
            out += chunk
    return bytes(out)


def parse_search(data: dict[str, Any]) -> list[DeezerTrack]:
    """Parse an api.deezer.com/search response."""
    tracks: list[DeezerTrack] = []
    for item in data.get("data", []):
        tracks.append(
            DeezerTrack(
                id=int(item["id"]),
                title=str(item.get("title", "Unknown")),
                artist=str(item.get("artist", {}).get("name", "Unknown")),
                album=str(item.get("album", {}).get("title", "")),
                duration_s=item.get("duration"),
                isrc=item.get("isrc"),
            )
        )
    return tracks


def select_track(tracks: list[DeezerTrack], query: str, isrc: str | None = None) -> DeezerTrack:
    """Prefer an exact ISRC match; else the first title-matching result."""
    if isrc:
        for track in tracks:
            if track.isrc and track.isrc.upper() == isrc.upper():
                return track
    for track in tracks:
        if title_matches(track.title, query):
            return track
    raise LookupError("no title-matching Deezer track")


def deezer_download(
    query: str,
    out_dir: Path,
    *,
    search: Search,
    resolve_url: ResolveUrl,
    get_bytes: GetBytes,
    isrc: str | None = None,
    clock: Clock = time.monotonic,
) -> DownloadOutcome:
    """Search Deezer, resolve the FLAC stream, download + Blowfish-decrypt it."""
    start = clock()
    track = select_track(parse_search(search(query)), query, isrc)
    encrypted = get_bytes(resolve_url(track.id))
    flac = decrypt_track(encrypted, blowfish_key(track.id))

    name = _UNSAFE.sub("_", f"{track.artist} - {track.title} [{track.id}]") + ".flac"
    dest = out_dir / name
    dest.write_bytes(flac)
    return DownloadOutcome(
        path=dest,
        tier=2,
        source="deezer",
        elapsed_s=clock() - start,
        query=query,
        title=track.title,
        artist=track.artist,
        album=track.album or None,
    )
