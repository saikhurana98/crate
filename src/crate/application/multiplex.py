"""Source multiplexing — the abstraction layer between source modules and tiers.

A tier is no longer a single source: it's a *pool* of swappable source modules
plus a strategy for how a request fans out across them:

  * ALL       — try every source in the pool (first verified file wins). Maximises
                coverage/redundancy; use when any one source may miss.
  * UNIFORM   — round-robin one source per request, so load spreads evenly across
                the pool (e.g. a 100-track playlist splits across 3 sources ~33
                each), reducing per-source rate-limit/ban risk. Falls through to
                the rest of the rotation only if the chosen source fails.

A source module is anything satisfying :class:`SourceEntry` (a name+tier handle
plus a ``download(query, out_dir)`` callable), so modules are swappable and the
tiers/cascade never depend on a concrete adapter.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from crate.application.download import DownloadReport
from crate.domain.audio import AudioInfo, classify
from crate.domain.download import DownloadOutcome
from crate.domain.tiers import SourceRef, Tier

Downloader = Callable[[str, Path], DownloadOutcome]
Prober = Callable[[Path], AudioInfo]


class MuxStrategy(Enum):
    ALL = "all"  # broadcast: try every source, first verified wins
    UNIFORM = "uniform"  # distribute: round-robin one source per request


@dataclass(frozen=True, slots=True)
class SourceEntry:
    """A swappable source module: identity + a download callable."""

    ref: SourceRef
    download: Downloader
    expected_lossless: bool


@dataclass(frozen=True, slots=True)
class CascadeAttempt:
    source: str
    tier: int
    ok: bool
    detail: str


@dataclass
class TierMux:
    """One tier's pool of sources plus its fan-out strategy."""

    tier: Tier
    sources: list[SourceEntry]
    strategy: MuxStrategy = MuxStrategy.ALL
    _cursor: int = field(default=0, repr=False)

    def _ordered(self) -> list[SourceEntry]:
        if self.strategy is MuxStrategy.UNIFORM and self.sources:
            i = self._cursor % len(self.sources)
            self._cursor += 1
            return self.sources[i:] + self.sources[:i]
        return list(self.sources)

    def acquire(
        self, query: str, out_dir: Path, *, prober: Prober
    ) -> tuple[DownloadReport | None, list[CascadeAttempt]]:
        """Fan out across the pool per strategy; return the first verified file."""
        attempts: list[CascadeAttempt] = []
        for source in self._ordered():
            try:
                outcome = source.download(query, out_dir)
                info = prober(outcome.path)
                verdict = classify(info)
            except Exception as exc:  # boundary: a source failure is a skipped attempt
                attempts.append(CascadeAttempt(source.ref.name, int(self.tier), False, str(exc)))
                continue

            if source.expected_lossless and not verdict.lossless:
                attempts.append(
                    CascadeAttempt(
                        source.ref.name,
                        int(self.tier),
                        False,
                        f"rejected: {verdict.label} (want lossless)",
                    )
                )
                continue

            attempts.append(CascadeAttempt(source.ref.name, int(self.tier), True, verdict.label))
            return DownloadReport(query, outcome, info, verdict, None), attempts

        return None, attempts
