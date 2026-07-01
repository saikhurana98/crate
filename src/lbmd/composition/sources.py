"""Composition root for source wiring.

Tier 1 = Monochrome (Amazon FLAC): a browser session (AmzSession) resolves the
fingerprint-bound stream, then httpx downloads the signed CloudFront URL and
mp4decrypt+ffmpeg produce the FLAC. Tier 3 = yt-dlp. Tier 2 is not registered
("skip Tier 2"), so the cascade goes Tier 1 -> Tier 3.

The Tier-1 browser is opt-in via ``LBMD_MONOCHROME=1`` (it launches a real
headed Chromium to pass Cloudflare Turnstile). When off, only Tier 3 runs.
This is the only layer allowed to bind concrete adapters + infrastructure.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from lbmd.adapters.sources import ytdlp
from lbmd.adapters.sources.monochrome import DEFAULT_TIDAL_BASE, monochrome_download
from lbmd.application.cascade import SourceEntry
from lbmd.domain.download import DownloadOutcome
from lbmd.domain.meta import TrackMeta
from lbmd.domain.tiers import SourceRef, Tier
from lbmd.infrastructure import http
from lbmd.infrastructure.audio.decrypt import mp4decrypt_cenc


def build_default_sources() -> list[SourceEntry]:  # pragma: no cover - env + IO wiring
    """Tier 1 = Monochrome (opt-in), Tier 3 = yt-dlp. Tier 2 skipped."""
    entries: list[SourceEntry] = []

    if os.environ.get("LBMD_MONOCHROME", "").lower() in ("1", "true", "yes"):
        from lbmd.adapters.metadata import musicbrainz, spotify
        from lbmd.infrastructure.browser.amz_session import AmzSession

        session = AmzSession()

        def search_json(query: str) -> Any:
            return http.get_json(f"{DEFAULT_TIDAL_BASE}/search/", {"s": query})

        def resolve_meta(query: str) -> TrackMeta | None:
            # Spotify link -> exact ISRC + clean metadata; else MusicBrainz. Best-effort.
            try:
                return spotify.resolve(query, get_json=http.get_json) or musicbrainz.resolve(
                    query, get_json=http.get_json
                )
            except Exception:
                return None

        def monochrome(query: str, out_dir: Path) -> DownloadOutcome:
            meta = resolve_meta(query)
            # Use the authority's clean title/artist for TIDAL search (esp. for a
            # Spotify URL, where the raw query is a link), and its ISRC to pick the
            # exact master.
            search_text = f"{meta.artist} {meta.title}" if meta else query
            isrc = meta.isrc if meta else None
            return monochrome_download(
                search_text,
                out_dir,
                search_json=search_json,
                amz_fetch=session.fetch_amz,
                get_bytes=http.get_bytes,
                decrypt=mp4decrypt_cenc,
                isrc=isrc,
            )

        entries.append(
            SourceEntry(SourceRef("monochrome", Tier.ONE), monochrome, expected_lossless=True)
        )

    entries.append(
        SourceEntry(SourceRef("ytdlp", Tier.THREE), ytdlp.download, expected_lossless=False)
    )
    return entries
