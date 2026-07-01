"""Tier-1 source adapter: Monochrome (Amazon Music path) -> 24-bit FLAC.

Primary Tier-1 source (ADR-0015). Reverse-engineered + verified against a real
monochrome.tf session. The working hybrid:

  1. metadata   GET {tidal_base}/search/?s=<query>   (unauth, plain HTTP)
  2. resolve    GET {amz_base}/api/track/?track=&duration=&album=&artist=&quality=UHD
                with a Cloudflare Turnstile JWT. The JWT is *fingerprint-bound*,
                so this call must be issued from the browser that minted it -> the
                ``amz_fetch`` callable is browser-backed (see infrastructure/browser).
  3. download   GET <signed CloudFront URL>  (no auth) -> CENC fMP4 (FLAC).
  4. decrypt    mp4decrypt(KID:key) + ffmpeg remux -> clean native FLAC.

Every pure step here (parse/select/KID-extract/orchestrate) is unit tested with
fakes; only the Turnstile mint + amz fetch (browser) and the decrypt (subprocess)
are injected I/O.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from lbmd.domain.download import DownloadOutcome

DEFAULT_TIDAL_BASE = "https://api.monochrome.tf"
DEFAULT_AMZ_BASE = "https://amz.geeked.wtf"

SearchJson = Callable[[str], Any]  # query -> TIDAL search JSON (unauth)
AmzFetch = Callable[[dict[str, Any]], Any]  # amz params -> track JSON (browser, auth)
GetBytes = Callable[[str], bytes]
Decrypt = Callable[[Path, str, str, Path], None]  # (enc, kid_hex, key_hex, out)
Clock = Callable[[], float]

_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_KID_BYTES = 16  # CENC default_KID is a 16-byte UUID


@dataclass(frozen=True, slots=True)
class MonoTrack:
    id: int
    title: str
    artist: str
    album: str
    duration_s: int | None
    hires: bool
    isrc: str | None


@dataclass(frozen=True, slots=True)
class AmzStream:
    stream_url: str
    decryption_key: str
    codec: str
    bit_depth: int | None
    sample_rate: int | None


def _is_hires(item: dict[str, Any]) -> bool:
    quality = str(item.get("audioQuality", "")).upper()
    tags = [str(t).upper() for t in item.get("mediaMetadata", {}).get("tags", [])]
    return "HI_RES" in quality or any("HIRES" in t or "HI_RES" in t for t in tags)


def parse_search(data: dict[str, Any]) -> list[MonoTrack]:
    """Extract track metadata from a Hi-Fi/TIDAL search response."""
    tracks: list[MonoTrack] = []
    for item in data.get("data", {}).get("items", []):
        artist = item.get("artist", {}).get("name")
        if not artist:
            artists = item.get("artists", [])
            artist = artists[0].get("name") if artists else "Unknown"
        tracks.append(
            MonoTrack(
                id=int(item["id"]),
                title=str(item.get("title", "Unknown")),
                artist=str(artist),
                album=str(item.get("album", {}).get("title", "")),
                duration_s=item.get("duration"),
                hires=_is_hires(item),
                isrc=item.get("isrc"),
            )
        )
    return tracks


def pick_best(tracks: list[MonoTrack]) -> MonoTrack:
    """Prefer a hi-res match; otherwise the first result."""
    if not tracks:
        raise LookupError("no Monochrome match")
    for track in tracks:
        if track.hires:
            return track
    return tracks[0]


def select(tracks: list[MonoTrack], isrc: str | None) -> MonoTrack:
    """Prefer the exact ISRC match (the right master); else fall back to hi-res.

    ISRC matching is non-regressive: when no result matches (or no ISRC is
    known) this behaves exactly like :func:`pick_best`.
    """
    if isrc:
        wanted = isrc.upper()
        for track in tracks:
            if track.isrc and track.isrc.upper() == wanted:
                return track
    return pick_best(tracks)


def build_amz_params(track: MonoTrack) -> dict[str, Any]:
    """Amazon track-lookup query params (asks for ULTRA_HD / 24-bit)."""
    return {
        "track": track.title,
        "duration": track.duration_s,
        "album": track.album,
        "artist": track.artist,
        "quality": "UHD",
    }


def parse_amz_stream(resp: dict[str, Any]) -> AmzStream:
    """Pick the highest-bit-depth FLAC rendition from the Amazon response."""
    flacs = [q for q in resp.get("available_qualities", []) if q.get("codec") == "flac"]
    if not flacs:
        raise RuntimeError("no FLAC quality offered by Amazon for this track")
    best = max(flacs, key=lambda q: (q.get("bitDepth") or 0, q.get("sampleRate") or 0))
    return AmzStream(
        stream_url=best.get("streamUrl") or resp["stream_url"],
        decryption_key=resp["decryption_key"],
        codec="flac",
        bit_depth=best.get("bitDepth"),
        sample_rate=best.get("sampleRate"),
    )


def parse_kid(mp4: bytes) -> str:
    """Read the default_KID (hex) from the CENC ``tenc`` box of a fragmented MP4.

    tenc payload: version(1) flags(3) reserved(1) default_byte_blocks(1)
    default_isProtected(1) default_Per_Sample_IV_Size(1) default_KID(16).
    """
    marker = mp4.find(b"tenc")
    if marker < 0:
        raise ValueError("no tenc box (file is not CENC-encrypted)")
    kid = mp4[marker + 12 : marker + 12 + _KID_BYTES]
    if len(kid) != _KID_BYTES:
        raise ValueError("truncated tenc box")
    return kid.hex()


def monochrome_download(
    query: str,
    out_dir: Path,
    *,
    search_json: SearchJson,
    amz_fetch: AmzFetch,
    get_bytes: GetBytes,
    decrypt: Decrypt,
    isrc: str | None = None,
    clock: Clock = time.monotonic,
) -> DownloadOutcome:
    """Resolve + download the best 24-bit FLAC for ``query`` via Monochrome.

    When ``isrc`` is given, the exact-matching master is chosen (ADR-0013 /
    SpotiFLAC-style); otherwise the hi-res heuristic applies.
    """
    start = clock()
    track = select(parse_search(search_json(query)), isrc)
    stream = parse_amz_stream(amz_fetch(build_amz_params(track)))

    stem = _UNSAFE.sub("_", f"{track.artist} - {track.title} [{track.id}]")
    enc = out_dir / f"{stem}.enc.mp4"
    flac = out_dir / f"{stem}.flac"
    try:
        enc.write_bytes(get_bytes(stream.stream_url))
        kid = parse_kid(enc.read_bytes())
        decrypt(enc, kid, stream.decryption_key, flac)
    finally:
        enc.unlink(missing_ok=True)
    return DownloadOutcome(
        path=flac, tier=1, source="monochrome", elapsed_s=clock() - start, query=query
    )
