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

from crate.domain.candidate import Candidate
from crate.domain.download import DownloadOutcome
from crate.domain.identity import TrackIdentity
from crate.domain.matching import artist_matches, duration_prefilter, match_score, title_matches

DEFAULT_TIDAL_BASE = "https://api.monochrome.tf"
DEFAULT_AMZ_BASE = "https://amz.geeked.wtf"

SearchJson = Callable[[str], Any]  # query -> TIDAL search JSON (unauth)
AmzFetch = Callable[[dict[str, Any]], Any]  # amz params -> track JSON (browser, auth)
Fetch = Callable[[str, Path], None]  # (url, dest) -> stream the encrypted MP4 to dest
Decrypt = Callable[[Path, str, str, Path], None]  # (enc, kid_hex, key_hex, out)
Clock = Callable[[], float]

_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_KID_BYTES = 16  # CENC default_KID is a 16-byte UUID
_KID_WINDOW = 1 << 20  # 1 MiB — the tenc box lives in the moov near the file head
_KID_WINDOW_MAX = 8 << 20  # 8 MiB — give up (and don't read the whole file) past this


@dataclass(frozen=True, slots=True)
class MonoTrack:
    id: int
    title: str
    artist: str
    album: str
    duration_s: int | None
    hires: bool
    cover_url: str | None = None


@dataclass(frozen=True, slots=True)
class AmzStream:
    stream_url: str
    decryption_key: str
    codec: str
    bit_depth: int | None
    sample_rate: int | None


def tidal_cover_url(cover: str | None) -> str | None:
    """Build the TIDAL resources cover URL from an album ``cover`` UUID.

    TIDAL exposes covers at
    ``https://resources.tidal.com/images/<uuid-with-slashes>/1280x1280.jpg``,
    where the dashes of the UUID are replaced by ``/``. Returns ``None`` when no
    cover UUID is present (art is then simply skipped, never fatal)."""
    if not cover:
        return None
    path = cover.replace("-", "/")
    return f"https://resources.tidal.com/images/{path}/1280x1280.jpg"


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
        album = item.get("album") or {}
        tracks.append(
            MonoTrack(
                id=int(item["id"]),
                title=str(item.get("title", "Unknown")),
                artist=str(artist),
                album=str(album.get("title", "")),
                duration_s=item.get("duration"),
                hires=_is_hires(item),
                cover_url=tidal_cover_url(album.get("cover")),
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


def select_by_title(
    tracks: list[MonoTrack],
    query: str,
    want_duration_s: int | None = None,
    want_artist: str | None = None,
) -> MonoTrack:
    """Keep results whose title matches the query, then pick the best.

    Candidates pass a title gate, then (when ``want_artist`` is known) an artist
    gate that drops a same-title song by a different artist — the fix for the real
    incident where Alfaaz — "Hai Mera Dil" landed Alka Yagnik — "Hai Mera Dil Tu"
    whose title matched and whose duration coincidentally lined up (monochrome can't
    match by ISRC). Survivors are prefiltered by ``want_duration_s`` (when known),
    then ranked by title+artist relevance with hi-res as the tiebreak. Raises
    LookupError when nothing matches, so the cascade advances to the next tier
    rather than accepting a wrong-song FLAC.
    """
    matching = [t for t in tracks if title_matches(t.title, query)]
    if want_artist:
        matching = [t for t in matching if artist_matches(t.artist, want_artist)]
    if not matching:
        raise LookupError("no title-matching Monochrome result")
    pool = duration_prefilter(matching, want_duration_s, lambda t: t.duration_s)
    return max(pool, key=lambda t: (match_score(t.title, t.artist, query), t.hires))


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


def read_kid(path: Path) -> str:
    """Find the default_KID by reading only the head of ``path``, never the whole file.

    Reads an initial 1 MiB window and scans for the ``tenc`` box; if it isn't in the
    window (or the KID straddles the edge) the window doubles up to 8 MiB before
    giving up. The tenc box lives in the ``moov`` near the file head, so 1 MiB
    almost always suffices — this avoids reading 50-200 MB of CENC fMP4 into RAM
    just to parse 16 bytes.
    """
    window = _KID_WINDOW
    with path.open("rb") as fh:
        while True:
            head = fh.read(window)
            try:
                return parse_kid(head)
            except ValueError:
                # Stop when we've read the whole file (a bigger window can't help)
                # or hit the cap; re-parse so the real error surfaces.
                if len(head) < window or window >= _KID_WINDOW_MAX:
                    return parse_kid(head)
            window *= 2
            fh.seek(0)


def _download_params(
    params: dict[str, Any],
    track_id: int,
    artist: str,
    title: str,
    out_dir: Path,
    *,
    amz_fetch: AmzFetch,
    fetch: Fetch,
    decrypt: Decrypt,
) -> Path:
    """Download-by-params core shared by the cascade path and the review
    re-download: resolve the Amazon stream for ``params``, stream the CENC fMP4,
    read its KID from a small head window, and decrypt to native FLAC. No search
    or TIDAL selection — the amz params fully identify the track."""
    stream = parse_amz_stream(amz_fetch(params))
    stem = _UNSAFE.sub("_", f"{artist} - {title} [{track_id}]")
    enc = out_dir / f"{stem}.enc.mp4"
    flac = out_dir / f"{stem}.flac"
    try:
        fetch(stream.stream_url, enc)
        kid = read_kid(enc)
        decrypt(enc, kid, stream.decryption_key, flac)
    finally:
        enc.unlink(missing_ok=True)
    return flac


def monochrome_download(
    query: str,
    out_dir: Path,
    want: TrackIdentity | None = None,
    *,
    search_json: SearchJson,
    amz_fetch: AmzFetch,
    fetch: Fetch,
    decrypt: Decrypt,
    match_title: str | None = None,
    clock: Clock = time.monotonic,
) -> DownloadOutcome:
    """Resolve + download the best 24-bit FLAC for ``query`` via Monochrome.

    ``query`` drives the TIDAL search (title + artist gives better recall);
    ``match_title`` (if given) is what the title guard compares against, so an
    artist token in the query can't false-positive a wrong single-word title.
    ``want.duration_s`` (when known) prefilters the candidates.

    The encrypted fMP4 streams to disk (``fetch``); the KID is read from a small
    head window (``read_kid``) rather than the whole file.
    """
    start = clock()
    want_duration = want.duration_s if want else None
    want_artist = want.artist if want else None
    track = select_by_title(
        parse_search(search_json(query)), match_title or query, want_duration, want_artist
    )
    flac = _download_params(
        build_amz_params(track),
        track.id,
        track.artist,
        track.title,
        out_dir,
        amz_fetch=amz_fetch,
        fetch=fetch,
        decrypt=decrypt,
    )
    return DownloadOutcome(
        path=flac,
        tier=1,
        source="monochrome",
        elapsed_s=clock() - start,
        query=query,
        title=track.title,
        artist=track.artist,
        album=track.album or None,
        art_url=track.cover_url,
    )


def _candidate_track(candidate: Candidate) -> MonoTrack:
    """Rebuild the MonoTrack needed for the Amazon lookup from a stored candidate —
    the amz params are pure metadata (title/artist/album/duration), so no TIDAL
    re-search is needed to re-download an already-chosen result."""
    return MonoTrack(
        id=int(candidate.ref),
        title=candidate.title,
        artist=candidate.artist,
        album=candidate.album or "",
        duration_s=candidate.duration_s,
        hires=candidate.lossless,
        cover_url=None,
    )


def search_candidates(
    query: str,
    want: TrackIdentity | None = None,
    *,
    search_json: SearchJson,
) -> list[Candidate]:
    """Every title-matching Monochrome result as a :class:`Candidate` (no download).

    Reuses ``parse_search`` + the shared title gate (no ``select_by_title``
    collapse). ``ref`` is the TIDAL track id; the Amazon lookup is driven by the
    candidate's metadata, so ``download_candidate`` needs no re-search."""
    tracks = parse_search(search_json(query))
    return [
        Candidate(
            source="monochrome",
            tier=1,
            ref=str(t.id),
            title=t.title,
            artist=t.artist,
            album=t.album or None,
            quality_label="FLAC 24" if t.hires else "FLAC 16",
            lossless=True,
            duration_s=t.duration_s,
            size_bytes=None,
        )
        for t in tracks
        if title_matches(t.title, query)
    ]


def download_candidate(
    candidate: Candidate,
    out_dir: Path,
    want: TrackIdentity | None = None,
    *,
    amz_fetch: AmzFetch,
    fetch: Fetch,
    decrypt: Decrypt,
    clock: Clock = time.monotonic,
) -> DownloadOutcome:
    """Fetch exactly ``candidate`` via the Amazon path — no TIDAL search."""
    start = clock()
    track = _candidate_track(candidate)
    flac = _download_params(
        build_amz_params(track),
        track.id,
        track.artist,
        track.title,
        out_dir,
        amz_fetch=amz_fetch,
        fetch=fetch,
        decrypt=decrypt,
    )
    return DownloadOutcome(
        path=flac,
        tier=1,
        source="monochrome",
        elapsed_s=clock() - start,
        query=f"{candidate.artist} - {candidate.title}",
        title=candidate.title,
        artist=candidate.artist,
        album=candidate.album,
    )
