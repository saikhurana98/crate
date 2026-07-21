"""Source credential validation — download a known song to prove a source works.

The console's Sources panel needs a "test these credentials" button. Searching
alone isn't enough (a stale Qobuz token passes ``search`` but 401s at
``getFileUrl``), so validation runs the *full* per-source path on a well-known
track: search -> pick the best candidate -> download-by-ref to a temp dir ->
probe -> delete. Reaching the download step exercises exactly the cred-gated calls
(the signed-URL / ARL / JWT resolve), so a bad credential surfaces as a failure
with the real error text.

All IO is injected (the source's ``search``/``download`` halves + a prober), so
the decision tree is unit-tested without network, disk, or a browser.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from crate.domain.audio import AudioInfo
from crate.domain.candidate import Candidate
from crate.domain.identity import TrackIdentity
from crate.domain.matching import candidate_match

# A ubiquitous track every catalogue carries — the probe payload for validation.
TEST_QUERY = "Daft Punk Get Lucky"
TEST_IDENTITY = TrackIdentity(artist="Daft Punk", title="Get Lucky")

SearchFn = Callable[[str, "TrackIdentity | None"], list[Candidate]]
DownloadFn = Callable[[Candidate, Path, "TrackIdentity | None"], Any]
Prober = Callable[[Path], AudioInfo]
Clock = Callable[[], float]


@dataclass(frozen=True, slots=True)
class ValidationResult:
    """Outcome of a source credential test."""

    ok: bool
    detail: str
    quality_label: str | None = None
    elapsed_s: float | None = None


def validate_source(
    *,
    search: SearchFn,
    download: DownloadFn,
    prober: Prober,
    out_dir: Path,
    clock: Clock = time.monotonic,
) -> ValidationResult:
    """Download the test track through one source; return pass/fail + detail.

    Never raises — every failure (no results, auth error, dead source) is caught
    and returned as ``ok=False`` with the error text, so the endpoint can render
    it. The downloaded file is always deleted (it's only a probe)."""
    start = clock()
    try:
        candidates = search(TEST_QUERY, TEST_IDENTITY)
        if not candidates:
            return ValidationResult(
                False, "no results for the test track", elapsed_s=_since(clock, start)
            )
        best = max(
            candidates,
            key=lambda c: candidate_match(c.title, c.artist, c.duration_s, TEST_IDENTITY),
        )
        outcome = download(best, out_dir, TEST_IDENTITY)
        try:
            info = prober(outcome.path)
        finally:
            Path(outcome.path).unlink(missing_ok=True)
        return ValidationResult(
            True,
            f"downloaded “{best.title}” — {info.codec}",
            quality_label=best.quality_label,
            elapsed_s=_since(clock, start),
        )
    except Exception as exc:  # surfaced to the operator, never fatal
        return ValidationResult(
            False, str(exc)[:200] or type(exc).__name__, elapsed_s=_since(clock, start)
        )


def _since(clock: Clock, start: float) -> float:
    return round(clock() - start, 2)
