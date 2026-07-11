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

from crate.domain.candidate import Candidate
from crate.domain.download import DownloadOutcome
from crate.domain.identity import TrackIdentity
from crate.domain.matching import artist_matches, duration_prefilter, match_score, title_matches

_SECRET = b"g4el58wc0zvf9na1"  # Deezer's fixed key-derivation secret
_IV = bytes(range(8))  # 00 01 02 03 04 05 06 07
_CHUNK = 2048

Search = Callable[[str], Any]  # query -> Deezer search JSON
ResolveUrl = Callable[[int], str]  # track id -> encrypted CDN url (client does token+get_url)
Fetch = Callable[[str, Path], None]  # (url, dest) -> stream the encrypted stream to dest
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
    cover_url: str | None = None
    preview_url: str | None = None


def blowfish_key(track_id: int) -> bytes:
    """Per-track Blowfish key: XOR of the two halves of md5(track_id) with the secret."""
    md5 = hashlib.md5(str(track_id).encode()).hexdigest()
    return bytes(ord(md5[i]) ^ ord(md5[i + 16]) ^ _SECRET[i] for i in range(16))


def _destripe(index: int, chunk: bytes, key: bytes) -> bytes:
    """One stripe of Deezer's CBC-stripe scheme: every 3rd full 2048-byte chunk is
    Blowfish-CBC encrypted, the rest (and any trailing partial chunk) is clear."""
    if index % 3 == 0 and len(chunk) == _CHUNK:
        return bytes(Blowfish.new(key, Blowfish.MODE_CBC, _IV).decrypt(chunk))
    return chunk


def decrypt_track(data: bytes, key: bytes) -> bytes:
    """Reverse Deezer's CBC-stripe in memory (bytes -> bytes). Thin wrapper over the
    per-stripe rule shared with :func:`decrypt_stream`."""
    out = bytearray()
    for index, start in enumerate(range(0, len(data), _CHUNK)):
        out += _destripe(index, data[start : start + _CHUNK], key)
    return bytes(out)


def decrypt_stream(src: Path, dst: Path, key: bytes) -> None:
    """Reverse Deezer's CBC-stripe file -> file, 2048 bytes at a time.

    Same rule as :func:`decrypt_track` but streamed, so a 50-200 MB FLAC is never
    held in RAM (neither the encrypted nor the decrypted copy)."""
    with src.open("rb") as fin, dst.open("wb") as fout:
        index = 0
        while chunk := fin.read(_CHUNK):
            fout.write(_destripe(index, chunk, key))
            index += 1


def parse_search(data: dict[str, Any]) -> list[DeezerTrack]:
    """Parse an api.deezer.com/search response."""
    tracks: list[DeezerTrack] = []
    for item in data.get("data", []):
        album = item.get("album") or {}
        # Deezer's album carries several cover sizes; prefer 1000px cover_xl,
        # then cover_big — the librarian caps the fetch so a big URL is fine.
        cover = album.get("cover_xl") or album.get("cover_big")
        tracks.append(
            DeezerTrack(
                id=int(item["id"]),
                title=str(item.get("title", "Unknown")),
                artist=str(item.get("artist", {}).get("name", "Unknown")),
                album=str(album.get("title", "")),
                duration_s=item.get("duration"),
                isrc=item.get("isrc"),
                cover_url=cover or None,
                preview_url=item.get("preview") or None,  # 30 s public CDN clip
            )
        )
    return tracks


def select_track(
    tracks: list[DeezerTrack],
    query: str,
    isrc: str | None = None,
    want_duration_s: int | None = None,
    want_artist: str | None = None,
) -> DeezerTrack:
    """Prefer an exact ISRC match; else the best title+artist match.

    An exact ISRC short-circuits everything. Otherwise candidates pass a title gate,
    then (when ``want_artist`` is known) an artist gate that drops a same-title song
    by a different artist, then the duration prefilter, and the best title+artist
    match wins so a same-title song by a different artist isn't picked over the
    requested one."""
    if isrc:
        for track in tracks:
            if track.isrc and track.isrc.upper() == isrc.upper():
                return track
    matching = [t for t in tracks if title_matches(t.title, query)]
    if want_artist:
        matching = [t for t in matching if artist_matches(t.artist, want_artist)]
    if not matching:
        raise LookupError("no title-matching Deezer track")
    pool = duration_prefilter(matching, want_duration_s, lambda t: t.duration_s)
    return max(pool, key=lambda t: match_score(t.title, t.artist, query))


def _download_ref(
    track_id: int,
    artist: str,
    title: str,
    out_dir: Path,
    *,
    resolve_url: ResolveUrl,
    fetch: Fetch,
) -> Path:
    """Download-by-id core shared by the cascade path and the review re-download:
    resolve the encrypted stream for ``track_id``, stream it to disk, and
    Blowfish-decrypt stripe-by-stripe — skipping search + selection."""
    name = _UNSAFE.sub("_", f"{artist} - {title} [{track_id}]") + ".flac"
    dest = out_dir / name
    enc = dest.with_name(dest.name + ".enc.part")
    part = dest.with_name(dest.name + ".part")
    try:
        fetch(resolve_url(track_id), enc)
        decrypt_stream(enc, part, blowfish_key(track_id))
        part.replace(dest)
    finally:
        enc.unlink(missing_ok=True)
        part.unlink(missing_ok=True)
    return dest


def deezer_download(
    query: str,
    out_dir: Path,
    want: TrackIdentity | None = None,
    *,
    search: Search,
    resolve_url: ResolveUrl,
    fetch: Fetch,
    isrc: str | None = None,
    clock: Clock = time.monotonic,
) -> DownloadOutcome:
    """Search Deezer, resolve the FLAC stream, stream it to disk + Blowfish-decrypt.

    ``want`` (when known) supplies the ISRC short-circuit + duration prefilter. The
    encrypted body streams to ``<dest>.enc.part``, decrypts stripe-by-stripe to
    ``<dest>.part``, then renames to the final name — so neither the encrypted nor
    the decrypted file is ever fully buffered in RAM, and a failure leaves no half
    file. Both temporaries are removed in ``finally``."""
    start = clock()
    want_isrc = isrc or (want.isrc if want else None)
    want_duration = want.duration_s if want else None
    want_artist = want.artist if want else None
    track = select_track(parse_search(search(query)), query, want_isrc, want_duration, want_artist)

    dest = _download_ref(
        track.id, track.artist, track.title, out_dir, resolve_url=resolve_url, fetch=fetch
    )
    return DownloadOutcome(
        path=dest,
        tier=2,
        source="deezer",
        elapsed_s=clock() - start,
        query=query,
        title=track.title,
        artist=track.artist,
        album=track.album or None,
        art_url=track.cover_url,
    )


def search_candidates(
    query: str,
    want: TrackIdentity | None = None,
    *,
    search: Search,
) -> list[Candidate]:
    """Every title-matching Deezer result as a :class:`Candidate` (no download).

    Reuses ``parse_search`` + the shared title gate (no ``select_track`` collapse);
    each candidate's ``ref`` is the Deezer track id for later download-by-id."""
    tracks = parse_search(search(query))
    return [
        Candidate(
            source="deezer",
            tier=2,
            ref=str(t.id),
            title=t.title,
            artist=t.artist,
            album=t.album or None,
            quality_label="FLAC 16/44",
            lossless=True,
            duration_s=t.duration_s,
            size_bytes=None,
            preview_url=t.preview_url,
        )
        for t in tracks
        if title_matches(t.title, query)
    ]


def download_candidate(
    candidate: Candidate,
    out_dir: Path,
    want: TrackIdentity | None = None,
    *,
    resolve_url: ResolveUrl,
    fetch: Fetch,
    clock: Clock = time.monotonic,
) -> DownloadOutcome:
    """Fetch exactly ``candidate`` (by its Deezer track-id ``ref``) — no search."""
    start = clock()
    dest = _download_ref(
        int(candidate.ref),
        candidate.artist,
        candidate.title,
        out_dir,
        resolve_url=resolve_url,
        fetch=fetch,
    )
    return DownloadOutcome(
        path=dest,
        tier=2,
        source="deezer",
        elapsed_s=clock() - start,
        query=f"{candidate.artist} - {candidate.title}",
        title=candidate.title,
        artist=candidate.artist,
        album=candidate.album,
    )
