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
from crate.domain.identity import TrackIdentity, from_query
from crate.domain.matching import candidate_match

# A ubiquitous track every catalogue carries — the probe payload for validation.
TEST_QUERY = "Daft Punk Get Lucky"
TEST_IDENTITY = TrackIdentity(artist="Daft Punk", title="Get Lucky")

SearchFn = Callable[[str, "TrackIdentity | None"], list[Candidate]]
DownloadFn = Callable[[Candidate, Path, "TrackIdentity | None"], Any]
# The cascade's own download half: a source resolves the query itself rather than
# taking a candidate ref. Every source has one; only some have a candidate path.
DirectDownloadFn = Callable[[str, Path, "TrackIdentity | None"], Any]
Prober = Callable[[Path], AudioInfo]
Clock = Callable[[], float]
# An acquisition strategy: fetch into ``dest``, return (outcome, label, quality).
Acquire = Callable[[Path], "tuple[Any, str, str | None]"]


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
    query: str = TEST_QUERY,
    want: TrackIdentity | None = TEST_IDENTITY,
    clock: Clock = time.monotonic,
) -> ValidationResult:
    """Test one source via its candidate path: search -> pick best -> download-by-ref.

    ``query``/``want`` default to the well-known test track; pass a specific track
    to force *that* download through this source instead (the operator's
    "does this source work for X?" probe)."""

    def acquire(dest: Path) -> tuple[Any, str, str | None]:
        candidates = search(query, want)
        if not candidates:
            raise _NoResults
        # rank against the known identity, or one parsed from the free-text query
        target = want or from_query(query)
        best = max(
            candidates, key=lambda c: candidate_match(c.title, c.artist, c.duration_s, target)
        )
        return download(best, dest, want), best.title, best.quality_label

    return _validate(acquire, prober=prober, out_dir=out_dir, clock=clock)


def validate_source_direct(
    *,
    download: DirectDownloadFn,
    prober: Prober,
    out_dir: Path,
    query: str = TEST_QUERY,
    want: TrackIdentity | None = TEST_IDENTITY,
    clock: Clock = time.monotonic,
) -> ValidationResult:
    """Test one source via the cascade's own download half (query in, file out).

    The fallback for sources with no candidate/search-by-ref path — Soulseek and
    Telegram resolve a free-text query internally — so *every* source is testable,
    not just the ones the review tab can enumerate."""

    def acquire(dest: Path) -> tuple[Any, str, str | None]:
        outcome = download(query, dest, want)
        return outcome, Path(outcome.path).name, None

    return _validate(acquire, prober=prober, out_dir=out_dir, clock=clock)


def _validate(acquire: Acquire, *, prober: Prober, out_dir: Path, clock: Clock) -> ValidationResult:
    """Run one acquisition, probe the file, delete it, and report.

    Never raises — every failure (no results, auth error, dead source) is caught
    and returned as ``ok=False`` with the error text, so the endpoint can render
    it. The downloaded file is always deleted (it's only a probe)."""
    start = clock()
    try:
        outcome, label, quality = acquire(out_dir)
        try:
            info = prober(outcome.path)
        finally:
            Path(outcome.path).unlink(missing_ok=True)
        return ValidationResult(
            True,
            f"downloaded “{label}” — {info.codec}",
            quality_label=quality,
            elapsed_s=_since(clock, start),
        )
    except _NoResults:
        return ValidationResult(
            False, "no results for the test track", elapsed_s=_since(clock, start)
        )
    except Exception as exc:  # surfaced to the operator, never fatal
        return ValidationResult(
            False, str(exc)[:200] or type(exc).__name__, elapsed_s=_since(clock, start)
        )


class _NoResults(Exception):
    """The source returned an empty candidate list (not an error, just nothing)."""


def _since(clock: Clock, start: float) -> float:
    return round(clock() - start, 2)
