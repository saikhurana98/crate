"""User-assisted re-download use-case (the Review tab).

When the user picks a specific candidate for a failed request or a lossy grab, the
serve queue worker runs this: download *exactly that candidate* (no cascade, no
selection), verify it, then either

  * **upgrade** a lossy library file — atomically replace it in place and point the
    request's download row at the same path (reusing the Phase-1 upgrade
    semantics), or
  * **fulfil** a failed request — finalize the new file into the library.

Either way the request ends ``done`` and drops out of the review list. All IO is
injected (``download`` / ``prober`` / ``finalizer`` / ``replace`` / the recorder)
so the decision tree is unit tested without a DB, network, or filesystem. On any
failure the caller (the queue worker) marks the request failed — this raises.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from crate.application.download import DownloadReport
from crate.application.librarian import Finalizer
from crate.domain.audio import AudioInfo, classify
from crate.domain.candidate import Candidate
from crate.domain.identity import TrackIdentity

# Download exactly one candidate (search already done): (candidate, out_dir, want).
DownloadCandidate = Callable[[Candidate, Path, "TrackIdentity | None"], Any]
Prober = Callable[[Path], AudioInfo]
Replace = Callable[[Path, Path], None]  # atomically move produced over an existing path


class ReviewRecorder(Protocol):
    """The RequestStore surface the review re-download drives (subset)."""

    def record_attempt(
        self, request_id: str, source: str, tier: int, ok: bool, detail: str
    ) -> None: ...
    def finish_request(self, request_id: str, report: DownloadReport) -> None: ...


def run_review_download(
    request_id: str,
    out_dir: Path,
    candidate: Candidate,
    *,
    want: TrackIdentity | None,
    is_upgrade: bool,
    target_path: str | None,
    download: DownloadCandidate,
    prober: Prober,
    finalizer: Finalizer | None,
    replace: Replace,
    recorder: ReviewRecorder,
) -> Path:
    """Download ``candidate`` and resolve ``request_id`` with it; return the final path.

    Raises on a download/probe failure so the caller marks the request failed."""
    outcome = download(candidate, out_dir, want)
    info = prober(outcome.path)
    verdict = classify(info)
    report = DownloadReport(f"{candidate.artist} - {candidate.title}", outcome, info, verdict, None)

    if is_upgrade and target_path is not None:
        # Replace the existing (lossy) library file in place; keep the request
        # pointed at that path and update its download row to the new quality.
        dest = Path(target_path)
        replace(outcome.path, dest)
        recorder.record_attempt(
            request_id, outcome.source, outcome.tier, True, "review: replaced file from candidate"
        )
        final = dataclasses.replace(report, outcome=dataclasses.replace(outcome, path=dest))
        recorder.finish_request(request_id, final)
        return dest

    # Fulfil a failed request: finalize the fresh file into the library (when a
    # library root is configured), then finish the request there.
    if finalizer is not None:
        report = finalizer(report)
    recorder.record_attempt(
        request_id, outcome.source, outcome.tier, True, "review: downloaded chosen candidate"
    )
    recorder.finish_request(request_id, report)
    assert report.outcome is not None  # finalizer never drops a present outcome
    return report.outcome.path
