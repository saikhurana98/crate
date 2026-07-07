"""Identity de-dup + lossless-upgrade decision tree (Phase 1).

All collaborators are fakes: a spy recorder, a scripted cascade ``run``, an
``exists`` predicate, and a ``replace`` recorder. No DB / network / filesystem."""

from pathlib import Path
from typing import Any

from crate.application.cascade import (
    CascadeAttempt,
    CascadeResult,
    MuxStrategy,
    SourceEntry,
    TierMux,
)
from crate.application.dedup import resolve_request
from crate.application.download import DownloadReport
from crate.domain.audio import AudioInfo, QualityVerdict
from crate.domain.download import DownloadOutcome
from crate.domain.identity import TrackIdentity
from crate.domain.tiers import SourceRef, Tier


class SpyRecorder:
    def __init__(self, existing: dict[str, Any] | None = None) -> None:
        self._existing = existing
        self.find_keys: list[Any] = []
        self.attempts: list[tuple[str, str, int, bool, str]] = []
        self.finished: list[tuple[str, DownloadReport]] = []
        self.duplicated: list[tuple[str, dict[str, Any]]] = []
        self.failed: list[tuple[str, str]] = []

    def find_completed(self, key: Any) -> dict[str, Any] | None:
        self.find_keys.append(key)
        return self._existing

    def record_attempt(self, rid: str, source: str, tier: int, ok: bool, detail: str) -> None:
        self.attempts.append((rid, source, tier, ok, detail))

    def open_request(self, query: str, identity: Any = None) -> str:
        return "rid"

    def complete_as_duplicate(self, rid: str, existing: dict[str, Any]) -> None:
        self.duplicated.append((rid, existing))

    def finish_request(self, rid: str, report: DownloadReport) -> None:
        self.finished.append((rid, report))

    def fail_request(self, rid: str, error: str) -> None:
        self.failed.append((rid, error))


def _tier(n: int) -> TierMux:
    tier = Tier(n)
    dl = lambda q, d, w=None: DownloadOutcome(Path("/x.flac"), n, "s", 1.0, q)  # noqa: E731
    return TierMux(tier, [SourceEntry(SourceRef(f"s{n}", tier), dl, True)], MuxStrategy.ALL)


ALL_TIERS = [_tier(1), _tier(2), _tier(3)]


def _report(path: str, *, source: str, tier: int, lossless: bool) -> DownloadReport:
    outcome = DownloadOutcome(Path(path), tier, source, 2.0, "q")
    info = AudioInfo("flac" if lossless else "opus", "x", 48000, 2, 24, 1_000, 200.0, 1_000)
    verdict = QualityVerdict("lossless" if lossless else "lossy", lossless)
    return DownloadReport("q", outcome, info, verdict, None)


def _winner(
    path: str, source: str, tier: int, lossless: bool, attempts: list[CascadeAttempt] | None = None
) -> CascadeResult:
    report = _report(path, source=source, tier=tier, lossless=lossless)
    return CascadeResult("q", report, attempts or [])


def _no_winner(attempts: list[CascadeAttempt] | None = None) -> CascadeResult:
    return CascadeResult("q", None, attempts or [])


# --- no prior copy: full cascade ---


def test_no_match_runs_full_cascade_with_all_tiers() -> None:
    rec = SpyRecorder(existing=None)
    seen: dict[str, Any] = {}

    def run(query, out_dir, tiers, **kw):  # type: ignore[no-untyped-def]
        seen["tiers"] = [int(t.tier) for t in tiers]
        seen["kw"] = kw
        return _winner("/dl/new.flac", "qobuz", 1, True)

    out = resolve_request(
        "q",
        Path("/out"),
        "rid",
        identity=TrackIdentity(title="T"),
        tiers=ALL_TIERS,
        recorder=rec,
        run=run,
        exists=lambda p: False,
        replace=lambda a, b: None,
    )
    assert out == Path("/dl/new.flac")
    assert seen["tiers"] == [1, 2, 3]  # all tiers, recorder attached
    assert seen["kw"]["recorder"] is rec and seen["kw"]["request_id"] == "rid"


def test_no_match_no_winner_raises() -> None:
    rec = SpyRecorder(existing=None)
    try:
        resolve_request(
            "q",
            Path("/out"),
            "rid",
            identity=None,
            tiers=ALL_TIERS,
            recorder=rec,
            run=lambda *a, **k: _no_winner(),
            exists=lambda p: False,
            replace=lambda a, b: None,
        )
    except RuntimeError as exc:
        assert "no source produced a file" in str(exc)
    else:  # pragma: no cover - must raise
        raise AssertionError("expected RuntimeError")


# --- lossless match: reuse only ---


def test_lossless_match_is_reused_without_downloading() -> None:
    existing = {"path": "/nas/song.flac", "lossless": 1, "source_won": "qobuz", "tier_won": 1}
    rec = SpyRecorder(existing=existing)
    ran: list[Any] = []

    out = resolve_request(
        "q",
        Path("/out"),
        "rid",
        identity=TrackIdentity(isrc="US1"),
        tiers=ALL_TIERS,
        recorder=rec,
        run=lambda *a, **k: ran.append(1) or _no_winner(),  # type: ignore[func-returns-value]
        exists=lambda p: True,
        replace=lambda a, b: None,
    )
    assert out == Path("/nas/song.flac")
    assert ran == []  # never ran the cascade
    assert rec.duplicated == [("rid", existing)]
    assert rec.attempts[0][4] == "already in library — reused existing file"
    # find_completed keyed on the identity, not the raw query
    assert rec.find_keys == [TrackIdentity(isrc="US1")]


def test_lossless_match_but_file_gone_falls_through_to_cascade() -> None:
    existing = {"path": "/nas/gone.flac", "lossless": 1}
    rec = SpyRecorder(existing=existing)

    out = resolve_request(
        "q",
        Path("/out"),
        "rid",
        identity=TrackIdentity(title="T"),
        tiers=ALL_TIERS,
        recorder=rec,
        run=lambda *a, **k: _winner("/dl/fresh.flac", "qobuz", 1, True),
        exists=lambda p: False,  # library file vanished
        replace=lambda a, b: None,
    )
    assert out == Path("/dl/fresh.flac")
    assert rec.duplicated == []  # not reused


# --- lossy match: upgrade over tiers 1-2 ---


def test_lossy_match_upgrades_over_tiers_1_2_only() -> None:
    existing = {"path": "/nas/song.opus", "lossless": 0, "source_won": "ytdlp", "tier_won": 3}
    rec = SpyRecorder(existing=existing)
    replaced: list[tuple[Path, Path]] = []
    seen_tiers: dict[str, Any] = {}

    def run(query, out_dir, tiers, **kw):  # type: ignore[no-untyped-def]
        seen_tiers["tiers"] = [int(t.tier) for t in tiers]
        seen_tiers["kw"] = kw
        attempts = [CascadeAttempt("monochrome", 1, False, "no JWT")]
        return _winner("/dl/upgrade.flac", "qobuz", 1, True, attempts)

    out = resolve_request(
        "q",
        Path("/out"),
        "rid",
        identity=TrackIdentity(isrc="US1"),
        tiers=ALL_TIERS,
        recorder=rec,
        run=run,
        exists=lambda p: True,
        replace=lambda a, b: replaced.append((a, b)),
    )
    # winner replaced the existing library file in place; request points there
    assert out == Path("/nas/song.opus")
    assert replaced == [(Path("/dl/upgrade.flac"), Path("/nas/song.opus"))]
    assert seen_tiers["tiers"] == [1, 2]  # Tier 3 excluded
    assert seen_tiers["kw"]["recorder"] is None  # upgrade records manually
    assert seen_tiers["kw"]["finalizer"] is None  # replace in place, no library move
    # the sub-cascade attempts are copied through, plus the upgrade note
    assert ("rid", "monochrome", 1, False, "no JWT") in rec.attempts
    # finished at the existing path, and an "upgraded from lossy" note recorded
    assert rec.finished and str(rec.finished[0][1].outcome.path) == "/nas/song.opus"
    assert any(a[4] == "upgraded from lossy" for a in rec.attempts)


def test_lossy_match_upgrade_failure_keeps_the_lossy_file() -> None:
    existing = {"path": "/nas/song.opus", "lossless": 0, "source_won": "ytdlp", "tier_won": 3}
    rec = SpyRecorder(existing=existing)
    replaced: list[Any] = []

    fail_attempts = [CascadeAttempt("qobuz", 1, False, "not found")]
    out = resolve_request(
        "q",
        Path("/out"),
        "rid",
        identity=TrackIdentity(isrc="US1"),
        tiers=ALL_TIERS,
        recorder=rec,
        run=lambda *a, **k: _no_winner(fail_attempts),  # no lossless source found
        exists=lambda p: True,
        replace=lambda a, b: replaced.append((a, b)),
    )
    assert out == Path("/nas/song.opus")  # kept the existing lossy file
    assert replaced == []  # nothing moved, existing file untouched
    assert rec.duplicated == [("rid", existing)]  # completed as duplicate of the lossy file
    assert rec.finished == []
    assert ("rid", "qobuz", 1, False, "not found") in rec.attempts  # sub-cascade attempt copied
    assert any("no lossless source" in a[4] for a in rec.attempts)


def test_lossy_match_upgrade_winner_still_lossy_keeps_existing() -> None:
    # a Tier-1/2 source returned a file but it verified LOSSY -> not an upgrade
    existing = {"path": "/nas/song.opus", "lossless": 0, "source_won": "ytdlp", "tier_won": 3}
    rec = SpyRecorder(existing=existing)
    out = resolve_request(
        "q",
        Path("/out"),
        "rid",
        identity=TrackIdentity(isrc="US1"),
        tiers=ALL_TIERS,
        recorder=rec,
        run=lambda *a, **k: _winner("/dl/still-lossy.mp3", "soulseek", 2, False),
        exists=lambda p: True,
        replace=lambda a, b: (_ for _ in ()).throw(AssertionError("must not replace")),
    )
    assert out == Path("/nas/song.opus")
    assert rec.duplicated == [("rid", existing)]
