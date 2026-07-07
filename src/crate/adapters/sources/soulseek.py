"""Tier-2 source adapter: Soulseek (P2P) via slskd.

Talks to a running slskd daemon's REST API through an injected client: search
the network, pick the best title-matching / prefer-lossless file offered by a
peer with a free upload slot, enqueue it, and wait for the transfer. slskd
handles the Soulseek protocol; this adapter is pure selection + orchestration.

The client is injected as ``search`` (query -> slskd search responses) and
``download`` (chosen file -> local path), so all logic is unit tested.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from crate.domain.download import DownloadOutcome
from crate.domain.identity import TrackIdentity
from crate.domain.matching import (
    artist_matches,
    duration_prefilter,
    match_score,
    norm_tokens,
    title_matches,
)

Search = Callable[[str], list[dict[str, Any]]]  # query -> slskd response items
Download = Callable[["SlskFile", Path], Path]  # chosen file + out dir -> local path
Clock = Callable[[], float]

_LOSSLESS_EXT = frozenset({".flac", ".wav", ".aiff", ".alac", ".ape"})


@dataclass(frozen=True, slots=True)
class SlskFile:
    username: str
    filename: str  # remote path, often Windows-style with backslashes
    size: int
    bitrate_bps: int
    bit_depth: int | None
    length_s: int | None
    sample_rate: int | None
    free_slot: bool
    upload_speed: int


def _basename(remote_path: str) -> str:
    return remote_path.replace("\\", "/").rsplit("/", 1)[-1]


def _candidate_artist(filename: str) -> str:
    """Best-effort artist from a Soulseek filename: the part left of ' - ' in an
    'Artist - Title' stem, or the whole stem when there's no separator."""
    stem = Path(_basename(filename)).stem
    left, sep, _ = stem.partition(" - ")
    return left.strip() if sep else stem.strip()


def parse_responses(responses: list[dict[str, Any]]) -> list[SlskFile]:
    """Flatten slskd search responses (per-peer file lists) into candidates."""
    out: list[SlskFile] = []
    for resp in responses:
        for f in resp.get("files", []):
            out.append(
                SlskFile(
                    username=str(resp.get("username", "")),
                    filename=str(f.get("filename", "")),
                    size=int(f.get("size", 0)),
                    bitrate_bps=int(f.get("bitRate", 0)) * 1000,  # slskd reports kbps
                    bit_depth=f.get("bitDepth"),
                    length_s=f.get("length"),
                    sample_rate=f.get("sampleRate"),
                    free_slot=bool(resp.get("hasFreeUploadSlot", False)),
                    upload_speed=int(resp.get("uploadSpeed", 0)),
                )
            )
    return out


def is_lossless(file: SlskFile) -> bool:
    return Path(_basename(file.filename)).suffix.lower() in _LOSSLESS_EXT


def rank_files(
    candidates: list[SlskFile],
    query: str,
    min_bitrate_kbps: int = 0,
    want: TrackIdentity | None = None,
) -> list[SlskFile]:
    """Title-matching candidates above the bitrate floor, best first.

    When ``want.artist`` is known a wrong-artist file is gated out (its artist is
    derived best-effort from the filename), so a same-title file by a different
    artist can't survive on a peer that coincidentally reports a matching length.
    When ``want`` is known the pool is then duration-prefiltered (by the peer's
    reported ``length_s``), and the ranking leads with ``match_score`` of the
    filename against the query + artist tokens — so a same-title file by the wrong
    artist loses to the requested one. Remaining tiebreaks (unchanged): prefer
    lossless, a free upload slot, higher bitrate, faster peer."""
    floor = min_bitrate_kbps * 1000
    matching = [
        c
        for c in candidates
        if c.bitrate_bps >= floor and title_matches(Path(_basename(c.filename)).stem, query)
    ]
    artist = want.artist if want and want.artist else ""
    if artist:
        matching = [c for c in matching if artist_matches(_candidate_artist(c.filename), artist)]
    if want is not None:
        matching = duration_prefilter(matching, want.duration_s, lambda c: c.length_s)
    score_query = f"{query} {artist}".strip()

    def score(c: SlskFile) -> float:
        return match_score(Path(_basename(c.filename)).stem, artist, score_query)

    return sorted(
        matching,
        key=lambda c: (score(c), is_lossless(c), c.free_slot, c.bitrate_bps, c.upload_speed),
        reverse=True,
    )


def select_file(
    candidates: list[SlskFile],
    query: str,
    min_bitrate_kbps: int = 0,
    want: TrackIdentity | None = None,
) -> SlskFile:
    """Best single title-matching file. Raises LookupError when none match."""
    ranked = rank_files(candidates, query, min_bitrate_kbps, want)
    if not ranked:
        raise LookupError("no title-matching Soulseek file")
    return ranked[0]


def search_variants(query: str) -> list[str]:
    """Soulseek matches ALL query terms against filenames, so artist/stopword
    tokens (e.g. 'the', 'weeknd') often yield zero hits while the title alone
    matches hundreds. Try the cleaned full query, then drop the leading and the
    trailing token (covers both 'Artist Title' and 'Title Artist' orderings).
    Selection still guards with the ORIGINAL query, so reducing search terms
    can't let a wrong song through.
    """
    tokens = norm_tokens(query)
    if not tokens:
        return [query]
    candidates = [tokens, tokens[1:], tokens[:-1]]
    variants: list[str] = []
    for toks in candidates:
        text = " ".join(toks)
        if text and text not in variants:
            variants.append(text)
    return variants


def soulseek_download(
    query: str,
    out_dir: Path,
    want: TrackIdentity | None = None,
    *,
    search: Search,
    download: Download,
    min_bitrate_kbps: int = 0,
    max_peers: int = 4,
    clock: Clock = time.monotonic,
) -> DownloadOutcome:
    """Search Soulseek (retrying reduced queries), then try downloading from the
    best-ranked peers in turn — P2P transfers stall or get refused, so fall to
    the next peer rather than failing the whole source.

    ``want`` (when known) feeds the duration prefilter + artist-aware ranking."""
    start = clock()
    ranked: list[SlskFile] = []
    for variant in search_variants(query):
        ranked = rank_files(parse_responses(search(variant)), query, min_bitrate_kbps, want)
        if ranked:
            break
    if not ranked:
        raise LookupError("no title-matching Soulseek file")

    last_error: Exception | None = None
    for candidate in ranked[:max_peers]:
        try:
            path = download(candidate, out_dir)
        except Exception as exc:  # peer stalled/refused — try the next best
            last_error = exc
            continue
        return DownloadOutcome(
            path=path, tier=2, source="soulseek", elapsed_s=clock() - start, query=query
        )
    raise RuntimeError(f"all Soulseek peers failed: {last_error}")
