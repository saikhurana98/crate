"""Request-recording port (console/observability, ADR-0018).

The cascade use-case emits its lifecycle to a :class:`RequestRecorder` so a
request and its per-source attempt trail can be persisted for the management
console. The port is defined here (application owns the use-case boundary); the
SQLite implementation lives in ``infrastructure/sqlite`` and is wired in the
composition root. ``run_cascade`` takes an optional recorder, so the core stays
usable — and every existing test keeps passing — with no persistence attached.
"""

from __future__ import annotations

from typing import Protocol

from crate.application.download import DownloadReport
from crate.domain.identity import TrackIdentity


class RequestRecorder(Protocol):
    """Sink for the cascade lifecycle. All methods are best-effort side effects."""

    def open_request(self, query: str, identity: TrackIdentity | None = None) -> str:
        """Open a request row (status ``downloading``); return its id.

        ``identity`` (when known) persists the structured artist/title/ISRC/
        duration columns alongside the free-text ``query``."""
        ...

    def record_attempt(
        self, request_id: str, source: str, tier: int, ok: bool, detail: str
    ) -> None:
        """Append one source attempt to the trail."""
        ...

    def finish_request(self, request_id: str, report: DownloadReport) -> None:
        """Mark the request done and persist the winning download's facts."""
        ...

    def fail_request(self, request_id: str, error: str) -> None:
        """Mark the request failed with a reason (no tier produced a file)."""
        ...
