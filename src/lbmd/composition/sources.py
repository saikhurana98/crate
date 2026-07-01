"""Composition root for source wiring.

Registers the concrete Tier-1 (Monochrome/Amazon) and Tier-3 (yt-dlp) sources.
Tier 2 is intentionally not registered yet ("skip Tier 2"), so the cascade goes
straight from Tier 1 to Tier 3. This is the only layer allowed to bind concrete
adapters + infrastructure together.
"""

from __future__ import annotations

import os
from pathlib import Path

from lbmd.adapters.sources import ytdlp
from lbmd.adapters.sources.monochrome import monochrome_download
from lbmd.application.cascade import SourceEntry
from lbmd.domain.download import DownloadOutcome
from lbmd.domain.tiers import SourceRef, Tier
from lbmd.infrastructure import http
from lbmd.infrastructure.audio.decrypt import ffmpeg_cenc_decrypt


def build_default_sources() -> list[SourceEntry]:  # pragma: no cover - env + IO wiring
    """Tier 1 = Monochrome (Amazon FLAC), Tier 3 = yt-dlp. Tier 2 skipped."""
    jwt = os.environ.get("LBMD_MONOCHROME_JWT", "")

    def monochrome(query: str, out_dir: Path) -> DownloadOutcome:
        return monochrome_download(
            query,
            out_dir,
            get_json=http.get_json,
            get_bytes=http.get_bytes,
            decrypt=ffmpeg_cenc_decrypt,
            jwt=jwt,
        )

    return [
        SourceEntry(SourceRef("monochrome", Tier.ONE), monochrome, expected_lossless=True),
        SourceEntry(SourceRef("ytdlp", Tier.THREE), ytdlp.download, expected_lossless=False),
    ]
