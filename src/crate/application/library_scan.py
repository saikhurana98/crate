"""The ``dedup_scan`` job: keep an index of every audio file in the library.

One run:

1. **Walk** the library root and diff it against the index. New and changed files
   need analysis, a moved file keeps its id, and a vanished file is marked missing,
   never deleted.
2. **Analyse** each pending file: probe, tags, embedded cover, and one ``fpcalc``
   run. That gives the compressed fingerprint for AcoustID, decoded locally to the
   raw sub-fingerprints used for similarity.
3. **Look up** each fingerprint on AcoustID, rate-limited, keeping the AcoustID
   *track id*. Duplicate grouping keys on it.

All progress is in the DB, so the job resumes where it stopped after a restart,
and a failure on one file is recorded and skipped.

Safety: if the walk finds nothing while the index still has live files, the root
is probably unmounted, so the run stops before marking anything missing.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from crate.application.identify import AcoustidMatch, parse_acoustid_match
from crate.application.jobs import JobContext
from crate.domain.library import TrackTags
from crate.domain.library_scan import DiskEntry, IndexedFile, ScanPlan, plan_scan

_MAX_WARNINGS = 25  # per stage; the rest are counted, not logged
_LOOKUP_GIVE_UP = 5  # consecutive AcoustID failures -> stop looking up this run


@dataclass(frozen=True)
class FileAnalysis:
    codec: str
    container: str
    lossless: bool
    bitrate: int | None
    bit_depth: int | None
    sample_rate: int
    channels: int
    duration_s: float
    tags: TrackTags
    has_cover: bool
    fp_duration: int
    fp_compressed: str
    fp_raw: list[int]


Analyzer = Callable[[Path], FileAnalysis]
Lookup = Callable[[int, str], dict[str, Any]]
Walker = Callable[[Path], Iterable[DiskEntry]]


class LibraryIndexStore(Protocol):
    def indexed(self) -> list[IndexedFile]: ...
    def apply_plan(self, plan: ScanPlan) -> None: ...
    def pending_analysis(self) -> list[tuple[str, str]]: ...
    def save_analysis(self, file_id: str, analysis: FileAnalysis) -> None: ...
    def save_error(self, file_id: str, message: str) -> None: ...
    def pending_lookup(self) -> list[tuple[str, int, str]]: ...
    def save_lookup(self, file_id: str, match: AcoustidMatch | None) -> None: ...
    def stats(self) -> dict[str, int]: ...


class RateLimiter:
    """Spaces calls at least ``1 / rate`` seconds apart (AcoustID allows ~3/s)."""

    def __init__(
        self,
        rate: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._gap = 1.0 / rate if rate > 0 else 0.0
        self._clock = clock
        self._sleep = sleep
        self._next = 0.0

    def wait(self) -> None:
        now = self._clock()
        if self._next > now:
            self._sleep(self._next - now)
            now = self._next
        self._next = now + self._gap


def _walk(
    ctx: JobContext, root: Path, walk: Walker, store: LibraryIndexStore
) -> tuple[int, ScanPlan]:
    if not root.is_dir():
        raise RuntimeError(f"library root {root} is not a directory; is it mounted?")
    indexed = store.indexed()
    disk = list(walk(root))
    live = sum(1 for f in indexed if not f.missing)
    if not disk and live:
        raise RuntimeError(
            f"found no audio files under {root} but {live} are indexed; "
            "is the library mounted? Nothing was marked missing."
        )
    plan = plan_scan(indexed, disk)
    store.apply_plan(plan)
    ctx.log(
        f"walked {len(disk)} files: {len(plan.new)} new, {len(plan.changed)} changed, "
        f"{len(plan.moved)} moved, {len(plan.missing)} missing",
        code="walk",
        data={
            "files": len(disk),
            "new": len(plan.new),
            "changed": len(plan.changed),
            "moved": len(plan.moved),
            "missing": len(plan.missing),
        },
    )
    return len(disk), plan


def _analyze(
    ctx: JobContext, root: Path, store: LibraryIndexStore, analyze: Analyzer
) -> tuple[int, int]:
    pending = store.pending_analysis()
    failed = 0
    for done, (file_id, rel) in enumerate(pending, 1):
        ctx.check_cancelled()
        try:
            store.save_analysis(file_id, analyze(root / rel))
        except Exception as exc:  # one bad file must not stop the scan
            failed += 1
            store.save_error(file_id, str(exc)[:300])
            if failed <= _MAX_WARNINGS:
                ctx.warn(f"{rel}: {exc}", code="analyze_failed")
        ctx.progress(done, len(pending), f"analysing {done}/{len(pending)}")
    if failed > _MAX_WARNINGS:
        ctx.warn(f"{failed - _MAX_WARNINGS} more files failed analysis", code="analyze_failed")
    return len(pending), failed


def _lookup(
    ctx: JobContext, store: LibraryIndexStore, lookup: Lookup, limiter: RateLimiter
) -> tuple[int, int]:
    pending = store.pending_lookup()
    looked = identified = streak = 0
    for file_id, duration, fp in pending:
        ctx.check_cancelled()
        limiter.wait()
        try:
            match = parse_acoustid_match(lookup(duration, fp))
        except Exception as exc:  # network / rate limit / API error: leave it pending
            streak += 1
            if streak >= _LOOKUP_GIVE_UP:
                ctx.warn(
                    f"AcoustID failed {streak} times in a row ({exc}); "
                    "the rest wait for the next scan",
                    code="lookup_stopped",
                )
                break
            continue
        streak = 0
        store.save_lookup(file_id, match)
        looked += 1
        identified += 1 if match else 0
        ctx.progress(looked, len(pending), f"AcoustID {looked}/{len(pending)}")
    return looked, identified


def run_dedup_scan(
    ctx: JobContext,
    root: Path,
    *,
    walk: Walker,
    store: LibraryIndexStore,
    analyze: Analyzer,
    lookup: Lookup | None = None,
    limiter: RateLimiter | None = None,
) -> dict[str, Any]:
    """The ``dedup_scan`` job body; returns the run summary."""
    files, plan = _walk(ctx, root, walk, store)
    analysed, failed = _analyze(ctx, root, store, analyze)
    looked = identified = 0
    if lookup is None:
        ctx.log("AcoustID lookups are off (no CRATE_ACOUSTID_KEY)", code="lookup_off")
    else:
        looked, identified = _lookup(ctx, store, lookup, limiter or RateLimiter(2.5))
    stats = store.stats()
    ctx.log(
        f"indexed {stats['files']} files ({stats['identified']} identified by AcoustID); "
        f"this run: {analysed} analysed, {failed} failed, {looked} looked up",
        code="summary",
    )
    return {
        "files": files,
        "new": len(plan.new),
        "changed": len(plan.changed),
        "moved": len(plan.moved),
        "missing": len(plan.missing),
        "analysed": analysed,
        "analyse_failed": failed,
        "looked_up": looked,
        "identified": identified,
        "index": stats,
    }
