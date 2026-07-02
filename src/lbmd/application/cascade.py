"""Tier-cascade use-case (ADR-0015).

Strict Tier 1 -> 2 -> 3 priority over a list of :class:`TierMux` pools. Each tier
multiplexes its own sources (see :mod:`lbmd.application.multiplex`); the cascade
asks each tier in priority order and the first tier to return a verified file
wins. Absent tiers are simply not in the list.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from lbmd.application.download import DownloadReport
from lbmd.application.multiplex import (
    CascadeAttempt,
    MuxStrategy,
    Prober,
    SourceEntry,
    TierMux,
)

__all__ = [
    "CascadeAttempt",
    "CascadeResult",
    "MuxStrategy",
    "SourceEntry",
    "TierMux",
    "run_cascade",
]


@dataclass(frozen=True, slots=True)
class CascadeResult:
    query: str
    winner: DownloadReport | None
    attempts: list[CascadeAttempt]


def run_cascade(
    query: str,
    out_dir: Path,
    tiers: list[TierMux],
    *,
    prober: Prober,
) -> CascadeResult:
    """Ask each tier in priority order; return the first verified winner."""
    attempts: list[CascadeAttempt] = []
    for tier in sorted(tiers, key=lambda t: t.tier.value):
        report, tier_attempts = tier.acquire(query, out_dir, prober=prober)
        attempts.extend(tier_attempts)
        if report is not None:
            return CascadeResult(query, report, attempts)
    return CascadeResult(query, None, attempts)
