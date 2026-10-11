"""Identity de-dup + lossless-upgrade use-case (Phase 1).

Before running the full cascade for a request, check whether the track is already
in the library (keyed on *identity* — ISRC or normalized artist+title — not the
raw query string). Three outcomes:

  * a **lossless** completed match exists (and the file is still on disk)
    -> reuse it: no download, mark the request done as a duplicate.
  * a **lossy** completed match exists -> run the cascade restricted to the
    lossless tiers (1-2). On a lossless win, finalize the new file to its
    canonical path, remove the old lossy copy, and note the upgrade. On no
    lossless win, keep the existing lossy file (request done as a duplicate).
  * no match -> run the full cascade as before.

All IO is injected (``find_completed`` / ``exists`` / ``run`` / ``replace`` /
the recorder methods) so the whole decision tree is unit tested without a DB,
network, or filesystem. Composition wires the concrete collaborators.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace as dc_replace
from pathlib import Path
from typing import Any, Protocol

from crate.application.cascade import CascadeResult
from crate.application.download import DownloadReport
from crate.application.multiplex import TierMux
from crate.domain.identity import TrackIdentity

# run the cascade for a request; the tiers list is already filtered by the caller.
RunCascade = Callable[..., CascadeResult]
# atomically move a produced file over an existing library path.
Replace = Callable[[Path, Path], None]
Remove = Callable[[Path], None]


class DedupRecorder(Protocol):
    """The RequestStore surface the de-dup path drives (subset of RequestStore)."""

    def find_completed(self, identity_or_query: TrackIdentity | str) -> dict[str, Any] | None: ...
    def record_attempt(
        self, request_id: str, source: str, tier: int, ok: bool, detail: str
    ) -> None: ...
    def open_request(self, query: str, identity: TrackIdentity | None = ...) -> str: ...
    def complete_as_duplicate(self, request_id: str, existing: dict[str, Any]) -> None: ...
    def finish_request(self, request_id: str, report: DownloadReport) -> None: ...
    def fail_request(self, request_id: str, error: str) -> None: ...


def _tiers_1_2(tiers: list[TierMux]) -> list[TierMux]:
    """The lossless tiers only (1 and 2) — the upgrade cascade skips Tier 3."""
    return [t for t in tiers if int(t.tier) in (1, 2)]


def resolve_request(  # noqa: PLR0913 - orchestration boundary with injected IO
    query: str,
    out_dir: Path,
    request_id: str,
    *,
    identity: TrackIdentity | None,
    tiers: list[TierMux],
    recorder: DedupRecorder,
    run: RunCascade,
    exists: Callable[[Path], bool],
    replace: Replace,
    finalizer: Any = None,
    remove: Remove | None = None,
) -> Path:
    """Resolve one request (identified by ``request_id``, already ``downloading``).

    Returns the path of the file that satisfies it (existing, upgraded, or freshly
    downloaded). Raises ``RuntimeError`` when nothing produces a file. The recorder
    is told exactly once whether the request finished, was a duplicate, or failed.
    """
    key: TrackIdentity | str = identity if identity is not None else query
    existing = recorder.find_completed(key)
    if existing is not None and exists(Path(str(existing["path"]))):
        if int(existing.get("lossless") or 0) == 1:
            return _reuse(request_id, existing, recorder)
        return _upgrade(
            query,
            out_dir,
            request_id,
            existing,
            identity=identity,
            tiers=tiers,
            recorder=recorder,
            run=run,
            replace=replace,
            finalizer=finalizer,
            remove=remove or (lambda path: path.unlink(missing_ok=True)),
        )

    # no prior copy — run the full cascade; the recorder finishes/fails the row.
    result = run(query, out_dir, tiers, request_id=request_id, recorder=recorder, want=identity)
    if result.winner is None or result.winner.outcome is None:
        raise RuntimeError(f"no source produced a file for {query!r}")
    return result.winner.outcome.path


def _reuse(request_id: str, existing: dict[str, Any], recorder: DedupRecorder) -> Path:
    """A lossless copy already exists — hand it back, no download."""
    recorder.record_attempt(
        request_id,
        str(existing.get("source_won") or "library"),
        int(existing.get("tier_won") or 0),
        True,
        "already in library — reused existing file",
    )
    recorder.complete_as_duplicate(request_id, existing)
    return Path(str(existing["path"]))


def _upgrade(  # noqa: PLR0913 - explicit collaborators keep this use-case testable
    query: str,
    out_dir: Path,
    request_id: str,
    existing: dict[str, Any],
    *,
    identity: TrackIdentity | None,
    tiers: list[TierMux],
    recorder: DedupRecorder,
    run: RunCascade,
    replace: Replace,
    finalizer: Any,
    remove: Remove,
) -> Path:
    """A lossy copy exists — race Tiers 1-2 for a lossless replacement.

    On a lossless win, run the configured librarian before removing the old file.
    On no win, keep the lossy file (done as a duplicate); the existing file is
    never deleted on failure."""
    lossless_tiers = _tiers_1_2(tiers)
    existing_path = Path(str(existing["path"]))
    # A configured librarian must see upgrades too. It writes tags/artwork and
    # gives the new codec its canonical suffix and library path. Without a
    # librarian (standalone use), retain the legacy in-place replacement.
    result = run(query, out_dir, lossless_tiers, recorder=None, want=identity, finalizer=finalizer)
    winner = result.winner
    if (
        winner is None
        or winner.outcome is None
        or winner.verdict is None
        or not winner.verdict.lossless
    ):
        # no lossless source — keep the lossy file we already have
        for a in result.attempts:
            recorder.record_attempt(request_id, a.source, a.tier, a.ok, a.detail)
        recorder.record_attempt(
            request_id,
            str(existing.get("source_won") or "library"),
            int(existing.get("tier_won") or 0),
            True,
            "no lossless source — kept existing lossy file",
        )
        recorder.complete_as_duplicate(request_id, existing)
        return existing_path

    # lossless win — after successful finalization, remove the obsolete lossy
    # copy. Never put FLAC bytes under the old .opus/.webm suffix.
    final_path = winner.outcome.path
    if finalizer is not None:
        if final_path != existing_path:
            remove(existing_path)
    else:
        replace(final_path, existing_path)
        final_path = existing_path
    for a in result.attempts:
        recorder.record_attempt(request_id, a.source, a.tier, a.ok, a.detail)
    recorder.record_attempt(
        request_id, winner.outcome.source, winner.outcome.tier, True, "upgraded from lossy"
    )
    final = dc_replace(winner, outcome=dc_replace(winner.outcome, path=final_path))
    recorder.finish_request(request_id, final)
    return final_path
