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
from lbmd.application.recording import RequestRecorder

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
    recorder: RequestRecorder | None = None,
) -> CascadeResult:
    """Ask each tier in priority order; return the first verified winner.

    When a ``recorder`` is supplied, the request and its per-source attempt trail
    are persisted (opened before the first tier, finished/failed at the end) so
    the management console can show live and historical downloads."""
    rid = recorder.open_request(query) if recorder is not None else None
    attempts: list[CascadeAttempt] = []
    for tier in sorted(tiers, key=lambda t: t.tier.value):
        report, tier_attempts = tier.acquire(query, out_dir, prober=prober)
        attempts.extend(tier_attempts)
        if recorder is not None and rid is not None:
            for a in tier_attempts:
                recorder.record_attempt(rid, a.source, a.tier, a.ok, a.detail)
        if report is not None:
            if recorder is not None and rid is not None:
                recorder.finish_request(rid, report)
            return CascadeResult(query, report, attempts)
    if recorder is not None and rid is not None:
        reason = attempts[-1].detail if attempts else "no sources produced a file"
        recorder.fail_request(rid, reason)
    return CascadeResult(query, None, attempts)
