"""Tier-1 source adapter: Monochrome (Amazon Music path) -> 24-bit FLAC.

Primary Tier-1 source (ADR-0015). Reverse-engineered from a real working
capture of monochrome.tf. The pipeline is:

  1. metadata  GET {tidal_base}/search/?s=<query>   (TIDAL search, no auth)
  2. stream    GET {amz_base}/api/track/?track=&duration=&album=&artist=&quality=UHD
               with header ``X-Turnstile-JWT`` -> stream_url + decryption_key +
               available_qualities (24-bit/48k FLAC as ULTRA_HD).
  3. download  GET <cloudfront stream_url>  -> CENC-encrypted fragmented MP4.
  4. decrypt   injected step (ffmpeg / instance /api/decrypt-stream) -> native FLAC.

Two real-world gates, both handled outside this pure module:
  * The ``X-Turnstile-JWT`` is a single-use Cloudflare Turnstile token; it must be
    minted in a browser (see composition / headless-browser acquirer) and passed in.
  * The stream is Amazon CENC (AES-CTR, subsample) — decryption is injected, since
    stock ffmpeg ``-decryption_key`` only partially decodes Amazon's scheme.

HTTP, clock, and decrypt are injected so parsing/selection/orchestration are
fully unit tested without network, tokens, or crypto.
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

# get_json(url, params, headers) -> parsed JSON
GetJson = Callable[[str, dict[str, Any], "dict[str, str] | None"], Any]
GetBytes = Callable[[str], bytes]
Decrypt = Callable[[Path, str, Path], None]
Clock = Callable[[], float]

_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


@dataclass(frozen=True, slots=True)
class MonoTrack:
    id: int
    title: str
    artist: str
    album: str
    duration_s: int | None
    hires: bool


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


def monochrome_download(
    query: str,
    out_dir: Path,
    *,
    get_json: GetJson,
    get_bytes: GetBytes,
    decrypt: Decrypt,
    jwt: str,
    tidal_base: str = DEFAULT_TIDAL_BASE,
    amz_base: str = DEFAULT_AMZ_BASE,
    clock: Clock = time.monotonic,
) -> DownloadOutcome:
    """Resolve + download the best 24-bit FLAC for ``query`` via Monochrome."""
    if not jwt:
        raise RuntimeError("Turnstile JWT required (mint in a browser; see composition)")
    start = clock()
    search = get_json(f"{tidal_base}/search/", {"s": query}, None)
    track = pick_best(parse_search(search))

    headers = {"X-Turnstile-JWT": jwt, "Origin": "https://monochrome.tf"}
    amz = get_json(f"{amz_base}/api/track/", build_amz_params(track), headers)
    stream = parse_amz_stream(amz)

    stem = _UNSAFE.sub("_", f"{track.artist} - {track.title} [{track.id}]")
    enc = out_dir / f"{stem}.enc.mp4"
    flac = out_dir / f"{stem}.flac"
    try:
        enc.write_bytes(get_bytes(stream.stream_url))
        decrypt(enc, stream.decryption_key, flac)
    finally:
        enc.unlink(missing_ok=True)
    return DownloadOutcome(
        path=flac, tier=1, source="monochrome", elapsed_s=clock() - start, query=query
    )
