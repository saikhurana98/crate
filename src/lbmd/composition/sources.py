"""Composition root — wires swappable source modules into per-tier pools.

Each tier is a :class:`TierMux` (a pool of sources + a fan-out strategy), so a
tier can load-balance across several sources. Strategy is configurable per tier
via ``LBMD_MUX_TIER{1,2,3}`` = ``all`` (default, try every source) or
``uniform``/``distribute`` (round-robin one source per request).

  Tier 1  Monochrome (Amazon 24-bit FLAC)   opt-in LBMD_MONOCHROME=1
  Tier 2  Deezer (16-bit FLAC via ARL)       opt-in LBMD_DEEZER=1
          Soulseek (via slskd)               opt-in LBMD_SOULSEEK=1
          Telegram music bot                 opt-in LBMD_TELEGRAM=1
  Tier 3  yt-dlp (lossy fallback)            always on

Only this layer binds concrete adapters + infrastructure.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from lbmd.adapters.sources import ytdlp
from lbmd.adapters.sources.monochrome import DEFAULT_TIDAL_BASE, monochrome_download
from lbmd.application.cascade import MuxStrategy, SourceEntry, TierMux
from lbmd.domain.download import DownloadOutcome
from lbmd.domain.tiers import SourceRef, Tier
from lbmd.infrastructure import http
from lbmd.infrastructure.audio.decrypt import mp4decrypt_cenc


def _enabled(name: str) -> bool:
    return os.environ.get(name, "").lower() in ("1", "true", "yes")


def _strategy(tier_num: int) -> MuxStrategy:
    value = os.environ.get(f"LBMD_MUX_TIER{tier_num}", "all").lower()
    return MuxStrategy.UNIFORM if value in ("uniform", "distribute") else MuxStrategy.ALL


def _monochrome_source() -> SourceEntry:  # pragma: no cover - IO wiring
    from lbmd.infrastructure.browser.amz_session import AmzSession

    session = AmzSession()

    def search_json(query: str) -> Any:
        return http.get_json(f"{DEFAULT_TIDAL_BASE}/search/", {"s": query})

    def monochrome(query: str, out_dir: Path) -> DownloadOutcome:
        return monochrome_download(
            query,
            out_dir,
            search_json=search_json,
            amz_fetch=session.fetch_amz,
            get_bytes=http.get_bytes,
            decrypt=mp4decrypt_cenc,
        )

    return SourceEntry(SourceRef("monochrome", Tier.ONE), monochrome, expected_lossless=True)


def _telegram_source() -> SourceEntry:  # pragma: no cover - IO wiring
    from lbmd.adapters.sources.telegram_bot import telegram_download
    from lbmd.infrastructure.telegram.telethon_client import TelegramBotClient

    client = TelegramBotClient(
        api_id=int(os.environ["LBMD_TG_API_ID"]),
        api_hash=os.environ["LBMD_TG_API_HASH"],
        session=os.environ["LBMD_TG_SESSION"],
        bot=os.environ["LBMD_TG_BOT"],
    )

    def telegram(query: str, out_dir: Path) -> DownloadOutcome:
        return telegram_download(query, out_dir, request=client.request, download=client.download)

    # accepts whatever the bot serves (prefers lossless in selection)
    return SourceEntry(SourceRef("telegram", Tier.TWO), telegram, expected_lossless=False)


def _deezer_source() -> SourceEntry:  # pragma: no cover - IO wiring
    from lbmd.adapters.sources.deezer import deezer_download
    from lbmd.infrastructure.deezer.client import DeezerClient

    client = DeezerClient(os.environ["LBMD_DEEZER_ARL"])

    def deezer(query: str, out_dir: Path) -> DownloadOutcome:
        return deezer_download(
            query,
            out_dir,
            search=client.search,
            resolve_url=client.resolve_url,
            get_bytes=client.get_bytes,
        )

    # Deezer always serves FLAC when available; if the account tier lacks it the
    # client raises and the cascade advances.
    return SourceEntry(SourceRef("deezer", Tier.TWO), deezer, expected_lossless=True)


def _soulseek_source() -> SourceEntry:  # pragma: no cover - IO wiring
    from lbmd.adapters.sources.soulseek import soulseek_download
    from lbmd.infrastructure.soulseek.slskd_client import SlskdClient

    client = SlskdClient(
        os.environ["LBMD_SLSKD_URL"],
        os.environ["LBMD_SLSKD_API_KEY"],
        os.environ["LBMD_SLSKD_DOWNLOADS"],
    )
    min_kbps = int(os.environ.get("LBMD_SLSKD_MIN_BITRATE", "0"))

    def soulseek(query: str, out_dir: Path) -> DownloadOutcome:
        return soulseek_download(
            query,
            out_dir,
            search=client.search,
            download=client.download,
            min_bitrate_kbps=min_kbps,
        )

    # prefers lossless in selection; a good Soulseek MP3 still beats Tier-3 YouTube
    return SourceEntry(SourceRef("soulseek", Tier.TWO), soulseek, expected_lossless=False)


def build_tiers() -> list[TierMux]:  # pragma: no cover - env + IO wiring
    """Build the per-tier source pools from configuration."""
    tier1 = [_monochrome_source()] if _enabled("LBMD_MONOCHROME") else []
    tier2: list[SourceEntry] = []
    if _enabled("LBMD_DEEZER"):
        tier2.append(_deezer_source())
    if _enabled("LBMD_SOULSEEK"):
        tier2.append(_soulseek_source())
    if _enabled("LBMD_TELEGRAM"):
        tier2.append(_telegram_source())
    tier3 = [SourceEntry(SourceRef("ytdlp", Tier.THREE), ytdlp.download, expected_lossless=False)]

    tiers: list[TierMux] = []
    if tier1:
        tiers.append(TierMux(Tier.ONE, tier1, _strategy(1)))
    if tier2:
        tiers.append(TierMux(Tier.TWO, tier2, _strategy(2)))
    tiers.append(TierMux(Tier.THREE, tier3, _strategy(3)))
    return tiers
