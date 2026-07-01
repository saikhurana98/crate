"""Tier cascade: try Tier 1, then (skipping absent tiers) the next, first
verified file wins (ADR-0015). Downloaders + prober are injected."""

from pathlib import Path

from lbmd.application.cascade import SourceEntry, run_cascade
from lbmd.domain.audio import AudioInfo
from lbmd.domain.download import DownloadOutcome
from lbmd.domain.tiers import SourceRef, Tier


def _outcome(source: str, tier: int, path: str) -> DownloadOutcome:
    return DownloadOutcome(Path(path), tier=tier, source=source, elapsed_s=1.0, query="q")


def _flac() -> AudioInfo:
    return AudioInfo("flac", "flac", 48000, 2, 24, 1_800_000, 110.0, 25_000_000)


def _opus() -> AudioInfo:
    return AudioInfo("opus", "webm", 48000, 2, None, 130000, 110.0, 3_000_000)


def _entry(name: str, tier: Tier, dl, lossless: bool):  # type: ignore[no-untyped-def]
    return SourceEntry(ref=SourceRef(name, tier), download=dl, expected_lossless=lossless)


def test_tier1_success_wins_and_skips_lower_tiers(tmp_path: Path) -> None:
    called: list[str] = []

    def mono(q: str, d: Path) -> DownloadOutcome:
        called.append("mono")
        return _outcome("monochrome", 1, "/x/a.flac")

    def ytdlp(q: str, d: Path) -> DownloadOutcome:
        called.append("ytdlp")
        return _outcome("ytdlp", 3, "/x/a.opus")

    entries = [
        _entry("monochrome", Tier.ONE, mono, True),
        _entry("ytdlp", Tier.THREE, ytdlp, False),
    ]
    result = run_cascade("Diljit Dealer", tmp_path, entries, prober=lambda p: _flac())

    assert result.winner is not None
    assert result.winner.outcome.source == "monochrome"
    assert result.winner.verdict.lossless is True
    assert called == ["mono"]  # Tier 3 never attempted
    assert [(a.tier, a.ok) for a in result.attempts] == [(1, True)]


def test_falls_through_to_tier3_when_tier1_fails(tmp_path: Path) -> None:
    def mono(q: str, d: Path) -> DownloadOutcome:
        raise RuntimeError("Turnstile JWT required")

    def ytdlp(q: str, d: Path) -> DownloadOutcome:
        return _outcome("ytdlp", 3, "/x/a.opus")

    entries = [
        _entry("monochrome", Tier.ONE, mono, True),
        _entry("ytdlp", Tier.THREE, ytdlp, False),
    ]
    result = run_cascade("q", tmp_path, entries, prober=lambda p: _opus())

    assert result.winner is not None
    assert result.winner.outcome.source == "ytdlp"
    # Tier 1 recorded as a failed attempt, Tier 3 as the win. Tier 2 absent (skipped).
    assert [(a.source, a.ok) for a in result.attempts] == [("monochrome", False), ("ytdlp", True)]
    assert "Turnstile" in result.attempts[0].detail


def test_tier1_nonlossless_is_rejected_and_advances(tmp_path: Path) -> None:
    def mono(q: str, d: Path) -> DownloadOutcome:
        return _outcome("monochrome", 1, "/x/a.flac")  # but probe says lossy

    def ytdlp(q: str, d: Path) -> DownloadOutcome:
        return _outcome("ytdlp", 3, "/x/a.opus")

    entries = [
        _entry("monochrome", Tier.ONE, mono, True),
        _entry("ytdlp", Tier.THREE, ytdlp, False),
    ]
    # prober reports lossy for BOTH; Tier-1 requires lossless so it is rejected.
    result = run_cascade("q", tmp_path, entries, prober=lambda p: _opus())

    assert result.winner is not None
    assert result.winner.outcome.source == "ytdlp"
    assert result.attempts[0].ok is False
    assert "rejected" in result.attempts[0].detail


def test_all_tiers_fail_returns_no_winner(tmp_path: Path) -> None:
    def boom(q: str, d: Path) -> DownloadOutcome:
        raise RuntimeError("down")

    entries = [_entry("monochrome", Tier.ONE, boom, True), _entry("ytdlp", Tier.THREE, boom, False)]
    result = run_cascade("q", tmp_path, entries, prober=lambda p: _flac())

    assert result.winner is None
    assert all(a.ok is False for a in result.attempts)
    assert len(result.attempts) == 2
