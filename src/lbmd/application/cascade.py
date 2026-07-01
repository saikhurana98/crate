"""Tier-cascade use-case (ADR-0015).

Runs the strict Tier 1 -> 2 -> 3 priority: sources are grouped into per-tier
race sets by :func:`plan_cascade`; each group is attempted in order and the
first file that downloads *and verifies* wins. Absent tiers are skipped simply
by not registering any source for them (that is how "skip Tier 2" works today).

Verification here is minimal: the file must probe successfully and, for a source
that promises lossless (Tier 1/2), the probe must confirm it — a fake/transcoded
"FLAC" is rejected and the cascade advances. Fingerprint identity + spectral
fake-FLAC detection (ADR-0005) are deferred.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from lbmd.application.download import DownloadReport
from lbmd.domain.audio import AudioInfo, classify
from lbmd.domain.download import DownloadOutcome
from lbmd.domain.tiers import SourceRef, plan_cascade

Downloader = Callable[[str, Path], DownloadOutcome]
Prober = Callable[[Path], AudioInfo]


@dataclass(frozen=True, slots=True)
class SourceEntry:
    ref: SourceRef
    download: Downloader
    expected_lossless: bool


@dataclass(frozen=True, slots=True)
class CascadeAttempt:
    source: str
    tier: int
    ok: bool
    detail: str


@dataclass(frozen=True, slots=True)
class CascadeResult:
    query: str
    winner: DownloadReport | None
    attempts: list[CascadeAttempt]


def run_cascade(
    query: str,
    out_dir: Path,
    entries: list[SourceEntry],
    *,
    prober: Prober,
) -> CascadeResult:
    """Attempt sources in strict tier order; return the first verified winner."""
    by_name = {e.ref.name: e for e in entries}
    attempts: list[CascadeAttempt] = []

    for group in plan_cascade([e.ref for e in entries]):
        for ref in group:
            entry = by_name[ref.name]
            try:
                outcome = entry.download(query, out_dir)
                info = prober(outcome.path)
                verdict = classify(info)
            except Exception as exc:  # boundary: a source failure is a skipped attempt
                attempts.append(CascadeAttempt(ref.name, int(ref.tier), False, str(exc)))
                continue

            if entry.expected_lossless and not verdict.lossless:
                attempts.append(
                    CascadeAttempt(
                        ref.name, int(ref.tier), False, f"rejected: {verdict.label} (want lossless)"
                    )
                )
                continue

            attempts.append(CascadeAttempt(ref.name, int(ref.tier), True, verdict.label))
            report = DownloadReport(query, outcome, info, verdict, None)
            return CascadeResult(query, report, attempts)

    return CascadeResult(query, None, attempts)
