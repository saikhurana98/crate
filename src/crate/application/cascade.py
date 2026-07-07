"""Tier-cascade use-case (ADR-0015).

Strict Tier 1 -> 2 -> 3 priority over a list of :class:`TierMux` pools. Each tier
multiplexes its own sources (see :mod:`crate.application.multiplex`); the cascade
asks each tier in priority order and the first tier to return a verified file
wins. Absent tiers are simply not in the list.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from crate.application.download import DownloadReport
from crate.application.librarian import Finalizer
from crate.application.multiplex import (
    CascadeAttempt,
    MuxStrategy,
    Prober,
    SourceEntry,
    TierMux,
)
from crate.application.recording import RequestRecorder

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
    request_id: str | None = None,
    finalizer: Finalizer | None = None,
) -> CascadeResult:
    """Ask each tier in priority order; return the first verified winner.

    When a ``recorder`` is supplied, the request and its per-source attempt trail
    are persisted (opened before the first tier, finished/failed at the end) so
    the management console can show live and historical downloads. Pass
    ``request_id`` to attach to a row that already exists (e.g. a console
    ``queued`` request the shim just claimed) instead of opening a new one.

    When a ``finalizer`` is supplied, the winning file is organized + tagged and
    moved into the music library the moment it verifies (per track, not per
    batch); the recorder then stores the final library path."""
    rid = (
        request_id
        if request_id is not None
        else (recorder.open_request(query) if recorder is not None else None)
    )
    attempts: list[CascadeAttempt] = []
    for tier in sorted(tiers, key=lambda t: t.tier.value):
        report, tier_attempts = tier.acquire(query, out_dir, prober=prober)
        attempts.extend(tier_attempts)
        if recorder is not None and rid is not None:
            for a in tier_attempts:
                recorder.record_attempt(rid, a.source, a.tier, a.ok, a.detail)
        if report is not None:
            if finalizer is not None:
                report = finalizer(report)
            if recorder is not None and rid is not None:
                recorder.finish_request(rid, report)
            return CascadeResult(query, report, attempts)
    if recorder is not None and rid is not None:
        reason = attempts[-1].detail if attempts else "no sources produced a file"
        recorder.fail_request(rid, reason)
    return CascadeResult(query, None, attempts)
