"""Download use-case: acquire a track and attach a quality verdict.

Only Tier 3 (yt-dlp) is wired today; the Tier 1/2 FLAC sources and the full
cascade scheduler (ADR-0015) are not implemented yet, so every result here is
lossy. The adapter and prober are injected for testing.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from crate.adapters.sources import ytdlp
from crate.adapters.sources.ytdlp import DownloadOutcome
from crate.domain.audio import AudioInfo, QualityVerdict, classify
from crate.infrastructure.audio import probe as probe_mod

Downloader = Callable[[str, Path], DownloadOutcome]
Prober = Callable[[Path], AudioInfo]


@dataclass(frozen=True, slots=True)
class DownloadReport:
    query: str
    outcome: DownloadOutcome | None
    info: AudioInfo | None
    verdict: QualityVerdict | None
    error: str | None

    @property
    def ok(self) -> bool:
        return self.error is None


def acquire(
    query: str,
    out_dir: Path,
    *,
    downloader: Downloader = ytdlp.download,
    prober: Prober = probe_mod.probe,
) -> DownloadReport:
    """Download one query, probe the file, classify quality. Never raises —
    failures are captured on the report."""
    try:
        outcome = downloader(query, out_dir)
        info = prober(outcome.path)
        return DownloadReport(query, outcome, info, classify(info), None)
    except Exception as exc:  # boundary: surface any failure as data
        return DownloadReport(query, None, None, None, str(exc))


def acquire_many(
    queries: list[str],
    out_dir: Path,
    *,
    downloader: Downloader = ytdlp.download,
    prober: Prober = probe_mod.probe,
) -> list[DownloadReport]:
    """Acquire each query in order, returning one report per query."""
    return [acquire(q, out_dir, downloader=downloader, prober=prober) for q in queries]
