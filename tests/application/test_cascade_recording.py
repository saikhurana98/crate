"""run_cascade emits its lifecycle to an injected RequestRecorder (ADR-0018).

A spy recorder captures the calls so we assert the cascade opens a request,
records every attempt, and finishes/fails exactly once — without any DB."""

from pathlib import Path

from crate.application.cascade import MuxStrategy, SourceEntry, TierMux, run_cascade
from crate.application.download import DownloadReport
from crate.domain.audio import AudioInfo
from crate.domain.download import DownloadOutcome
from crate.domain.identity import TrackIdentity
from crate.domain.tiers import SourceRef, Tier


class SpyRecorder:
    def __init__(self) -> None:
        self.opened: list[str] = []
        self.identities: list[TrackIdentity | None] = []
        self.attempts: list[tuple[str, int, bool, str]] = []
        self.finished: list[str] = []
        self.failed: list[tuple[str, str]] = []

    def open_request(self, query: str, identity: TrackIdentity | None = None) -> str:
        self.opened.append(query)
        self.identities.append(identity)
        return f"rid-{len(self.opened)}"

    def record_attempt(
        self, request_id: str, source: str, tier: int, ok: bool, detail: str
    ) -> None:
        self.attempts.append((source, tier, ok, detail))

    def finish_request(self, request_id: str, report: DownloadReport) -> None:
        self.finished.append(request_id)

    def fail_request(self, request_id: str, error: str) -> None:
        self.failed.append((request_id, error))


def _flac() -> AudioInfo:
    return AudioInfo("flac", "flac", 48000, 2, 24, 1_800_000, 110.0, 25_000_000)


def _tier(name: str, tier: Tier, dl, lossless: bool) -> TierMux:  # type: ignore[no-untyped-def]
    return TierMux(tier, [SourceEntry(SourceRef(name, tier), dl, lossless)], MuxStrategy.ALL)


def test_records_open_attempts_and_finish_on_win(tmp_path: Path) -> None:
    def mono(q: str, d: Path, w: TrackIdentity | None = None) -> DownloadOutcome:
        raise RuntimeError("Turnstile JWT required")

    def qobuz(q: str, d: Path, w: TrackIdentity | None = None) -> DownloadOutcome:
        return DownloadOutcome(Path("/x/a.flac"), 1, "qobuz", 2.0, q)

    spy = SpyRecorder()
    tiers = [_tier("monochrome", Tier.ONE, mono, True), _tier("qobuz", Tier.ONE, qobuz, True)]
    # two sources same tier: monochrome fails, qobuz wins (ALL strategy)
    result = run_cascade("Get Lucky", tmp_path, tiers, prober=lambda p: _flac(), recorder=spy)

    assert result.winner is not None
    assert spy.opened == ["Get Lucky"]
    assert ("monochrome", 1, False, "Turnstile JWT required") in spy.attempts
    assert ("qobuz", 1, True, "lossless 24-bit / 48 kHz flac") in spy.attempts
    assert spy.finished == ["rid-1"] and spy.failed == []


def test_records_failure_when_no_tier_wins(tmp_path: Path) -> None:
    def boom(q: str, d: Path, w: TrackIdentity | None = None) -> DownloadOutcome:
        raise RuntimeError("down")

    spy = SpyRecorder()
    tiers = [_tier("qobuz", Tier.ONE, boom, True), _tier("ytdlp", Tier.THREE, boom, False)]
    result = run_cascade("q", tmp_path, tiers, prober=lambda p: _flac(), recorder=spy)

    assert result.winner is None
    assert spy.opened == ["q"] and spy.finished == []
    assert len(spy.failed) == 1
    assert spy.failed[0][0] == "rid-1" and spy.failed[0][1] == "down"
    assert len(spy.attempts) == 2 and all(not a[2] for a in spy.attempts)


def test_request_id_attaches_to_existing_row_without_reopening(tmp_path: Path) -> None:
    def qobuz(q: str, d: Path, w: TrackIdentity | None = None) -> DownloadOutcome:
        return DownloadOutcome(Path("/x/a.flac"), 1, "qobuz", 2.0, q)

    spy = SpyRecorder()
    # the shim's queue worker claims a pre-created row and passes its id
    result = run_cascade(
        "q",
        tmp_path,
        [_tier("qobuz", Tier.ONE, qobuz, True)],
        prober=lambda p: _flac(),
        recorder=spy,
        request_id="claimed-42",
    )
    assert result.winner is not None
    assert spy.opened == []  # never opened a new request
    assert spy.finished == ["claimed-42"]


def test_no_recorder_is_a_clean_no_op(tmp_path: Path) -> None:
    def qobuz(q: str, d: Path, w: TrackIdentity | None = None) -> DownloadOutcome:
        return DownloadOutcome(Path("/x/a.flac"), 1, "qobuz", 2.0, q)

    # recorder omitted -> existing behaviour, no crash
    result = run_cascade(
        "q", tmp_path, [_tier("qobuz", Tier.ONE, qobuz, True)], prober=lambda p: _flac()
    )
    assert result.winner is not None
