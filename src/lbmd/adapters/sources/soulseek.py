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

from lbmd.domain.download import DownloadOutcome
from lbmd.domain.matching import title_matches

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


def select_file(candidates: list[SlskFile], query: str, min_bitrate_kbps: int = 0) -> SlskFile:
    """Keep title-matching candidates above the bitrate floor, then prefer
    lossless, a free upload slot, higher bitrate, faster peer.

    Raises LookupError when nothing matches (cascade advances a tier).
    """
    floor = min_bitrate_kbps * 1000
    matching = [
        c
        for c in candidates
        if c.bitrate_bps >= floor and title_matches(Path(_basename(c.filename)).stem, query)
    ]
    if not matching:
        raise LookupError("no title-matching Soulseek file")
    return max(
        matching,
        key=lambda c: (is_lossless(c), c.free_slot, c.bitrate_bps, c.upload_speed),
    )


def soulseek_download(
    query: str,
    out_dir: Path,
    *,
    search: Search,
    download: Download,
    min_bitrate_kbps: int = 0,
    clock: Clock = time.monotonic,
) -> DownloadOutcome:
    """Search Soulseek, pick the best file, download it via slskd."""
    start = clock()
    chosen = select_file(parse_responses(search(query)), query, min_bitrate_kbps)
    path = download(chosen, out_dir)
    return DownloadOutcome(
        path=path, tier=2, source="soulseek", elapsed_s=clock() - start, query=query
    )
