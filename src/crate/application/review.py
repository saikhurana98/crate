"""User-assisted re-download use-case (the Review tab).

When the user picks a specific candidate for a failed request or a lossy grab, the
serve queue worker runs this: download *exactly that candidate* (no cascade, no
selection), verify it, then either

  * **upgrade** a lossy library file — atomically replace it in place and point the
    request's download row at the same path (reusing the Phase-1 upgrade
    semantics), or
  * **fulfil** a failed request — finalize the new file into the library.

Either way the request ends ``done`` and drops out of the review list. All IO is
injected (``download`` / ``prober`` / ``finalizer`` / ``replace`` / ``fingerprinter``
/ the recorder) so the decision tree is unit tested without a DB, network, or
filesystem. ``run_review_download`` raises on a source/probe failure; the caller
routes that through :func:`handle_review_error`, which keeps an upgrade's existing
file (restoring it to ``done``) and otherwise fails the request with a clean,
attributed message — a transient source blip never discards a good download.

**Fingerprint gate (ADR-0019):** before an *upgrade* replaces the existing file, the
new download and the existing file are fingerprint-compared. Below ``threshold`` the
replace is refused with :class:`MatchTooLow` (the worker parks the request as
``needs_confirm``) so the user can't silently swap, say, a studio cut for the acoustic
version yt-dlp grabbed. ``force=True`` (the user's explicit "replace anyway") skips
the gate; ``fingerprinter=None`` (binary absent) skips it too — the feature is opt-in.
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
from crate.domain.fingerprint import THRESHOLD
from crate.domain.identity import TrackIdentity

# Download exactly one candidate (search already done): (candidate, out_dir, want).
DownloadCandidate = Callable[[Candidate, Path, "TrackIdentity | None"], Any]
Prober = Callable[[Path], AudioInfo]
Replace = Callable[[Path, Path], None]  # atomically move produced over an existing path
Fingerprinter = Callable[[Path], tuple[int, list[int]]]  # path -> (duration, raw fingerprint)
Similarity = Callable[[list[int], list[int]], float]  # two raw fingerprints -> 0..1


class MatchTooLow(Exception):
    """A review upgrade's new file didn't match the existing one closely enough.

    Carries the measured similarity so the worker can park the request as
    ``needs_confirm`` with the score for the user to override (ADR-0019)."""

    def __init__(self, score: float) -> None:
        super().__init__(f"fingerprint match {score:.2f} below threshold")
        self.score = score


class ReviewRecorder(Protocol):
    """The RequestStore surface the review re-download drives (subset)."""

    def record_attempt(
        self, request_id: str, source: str, tier: int, ok: bool, detail: str
    ) -> None: ...
    def finish_request(self, request_id: str, report: DownloadReport) -> None: ...
    def set_match_similarity(self, request_id: str, score: float) -> None: ...
    def restore_completed(self, request_id: str) -> bool: ...
    def fail_request(self, request_id: str, error: str) -> None: ...


def handle_review_error(
    request_id: str,
    candidate: Candidate,
    exc: BaseException,
    *,
    is_upgrade: bool,
    target_path: str | None,
    recorder: ReviewRecorder,
    exists: Callable[[Path], bool],
) -> None:
    """Recover gracefully when a review re-download fails on a **source/network**
    error (a transient upstream 401, a timeout — anything but the fingerprint gate,
    which the caller handles separately).

    The failure is recorded as an attempt against the candidate's source (so the
    trail is attributable, not an opaque "queue worker error"), then:

    * an **upgrade** whose existing file is still present is restored to ``done`` —
      the library file was never touched, so the track stays a normal lossy item
      that can be upgraded again once the source recovers, rather than a scary
      ``failed`` row;
    * otherwise (fulfilling a failed request, or the existing file vanished) the
      request is failed with a clean, attributed message.

    This mirrors the cascade's upgrade semantics ("keep the existing file on
    failure") so a momentary source blip can never discard a good download."""
    recorder.record_attempt(request_id, candidate.source, candidate.tier, False, f"review: {exc}")
    # An upgrade whose existing file is still present is restored to done (the file
    # was never touched); restore_completed returns False only if there's no row to
    # restore, in which case we fall through to a clean, attributed failure.
    if (
        is_upgrade
        and target_path
        and exists(Path(target_path))
        and recorder.restore_completed(request_id)
    ):
        return
    recorder.fail_request(request_id, f"review download from {candidate.source} failed: {exc}")


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
    fingerprinter: Fingerprinter | None = None,
    similarity: Similarity | None = None,
    threshold: float = THRESHOLD,
    force: bool = False,
) -> Path:
    """Download ``candidate`` and resolve ``request_id`` with it; return the final path.

    Raises on a download/probe failure so the caller marks the request failed, or
    :class:`MatchTooLow` when the upgrade fails the fingerprint gate."""
    outcome = download(candidate, out_dir, want)
    info = prober(outcome.path)
    verdict = classify(info)
    report = DownloadReport(f"{candidate.artist} - {candidate.title}", outcome, info, verdict, None)

    if is_upgrade and target_path is not None:
        # Replace the existing (lossy) library file in place; keep the request
        # pointed at that path and update its download row to the new quality.
        dest = Path(target_path)
        score: float | None = None
        if fingerprinter is not None and similarity is not None and not force and dest.exists():
            _, new_fp = fingerprinter(outcome.path)
            _, old_fp = fingerprinter(dest)
            score = similarity(new_fp, old_fp)
            if score < threshold:
                outcome.path.unlink(missing_ok=True)  # discard the rejected download
                raise MatchTooLow(score)
        if finalizer is not None:
            # Run upgrades through the same librarian as fresh downloads. The old
            # path often has a lossy suffix (for example .opus), so replacing it
            # byte-for-byte with FLAC both hid the real codec and skipped tags and
            # cover art. Finalize first while the old file is still safe; only
            # remove the old copy after tagging, artwork and the canonical move
            # have all succeeded.
            report = finalizer(report)
            assert report.outcome is not None
            final_path = report.outcome.path
            if final_path != dest:
                # If the canonical target was occupied by the old same-format
                # file, librarian chose " (2)". Collapse that temporary collision
                # back onto the stable path; otherwise keep the corrected path.
                collision = (
                    final_path.parent == dest.parent
                    and final_path.suffix.casefold() == dest.suffix.casefold()
                    and final_path.stem.startswith(f"{dest.stem} (")
                )
                if collision:
                    replace(final_path, dest)
                    report = dataclasses.replace(
                        report, outcome=dataclasses.replace(report.outcome, path=dest)
                    )
                else:
                    dest.unlink(missing_ok=True)
        else:
            replace(outcome.path, dest)
            report = dataclasses.replace(report, outcome=dataclasses.replace(outcome, path=dest))
        recorder.record_attempt(
            request_id,
            outcome.source,
            outcome.tier,
            True,
            "review: finalized upgrade from candidate",
        )
        if score is not None:
            recorder.set_match_similarity(request_id, score)
        recorder.finish_request(request_id, report)
        assert report.outcome is not None
        return report.outcome.path

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
