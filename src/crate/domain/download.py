"""Domain result type for a completed source download.

Shared by every source adapter (Tier 1/2/3) and the cascade use-case, so it
lives in the domain rather than in any one adapter.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class DownloadOutcome:
    path: Path
    tier: int
    source: str
    elapsed_s: float
    query: str
    # Metadata the source knew at download time (structured sources like Qobuz /
    # Deezer / Monochrome fill these). Used by the librarian to tag + file the
    # track; ``None`` means "unknown" — fall back to the file's own tags.
    artist: str | None = None
    album: str | None = None
    title: str | None = None
    track_no: int | None = None
    disc_no: int | None = None
    # Album cover URL the source already knew (structured sources fill it). The
    # librarian best-effort fetches it, embeds a front cover, and writes
    # ``cover.jpg`` beside the file; ``None`` means "no art known" (never fatal).
    art_url: str | None = None
