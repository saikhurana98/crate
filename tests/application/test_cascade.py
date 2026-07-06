"""Tier cascade over TierMux pools: strict Tier 1 -> 2 -> 3, first verified wins."""

from pathlib import Path

from crate.application.cascade import MuxStrategy, SourceEntry, TierMux, run_cascade
from crate.domain.audio import AudioInfo
from crate.domain.download import DownloadOutcome
from crate.domain.tiers import SourceRef, Tier


def _flac() -> AudioInfo:
    return AudioInfo("flac", "flac", 48000, 2, 24, 1_800_000, 110.0, 25_000_000)


def _opus() -> AudioInfo:
    return AudioInfo("opus", "webm", 48000, 2, None, 130000, 110.0, 3_000_000)


def _tier(name: str, tier: Tier, dl, lossless: bool) -> TierMux:  # type: ignore[no-untyped-def]
    return TierMux(tier, [SourceEntry(SourceRef(name, tier), dl, lossless)], MuxStrategy.ALL)


def test_tier1_wins_and_lower_tiers_not_touched(tmp_path: Path) -> None:
    called: list[str] = []

    def mono(q: str, d: Path) -> DownloadOutcome:
        called.append("mono")
        return DownloadOutcome(Path("/x/a.flac"), 1, "monochrome", 1.0, q)

    def ytdlp(q: str, d: Path) -> DownloadOutcome:
        called.append("ytdlp")
        return DownloadOutcome(Path("/x/a.opus"), 3, "ytdlp", 1.0, q)

    tiers = [_tier("monochrome", Tier.ONE, mono, True), _tier("ytdlp", Tier.THREE, ytdlp, False)]
    result = run_cascade("Diljit Dealer", tmp_path, tiers, prober=lambda p: _flac())

    assert result.winner is not None and result.winner.outcome.source == "monochrome"
    assert called == ["mono"]  # Tier 3 never reached
    assert [(a.tier, a.ok) for a in result.attempts] == [(1, True)]


def test_falls_through_tiers_skipping_absent_tier2(tmp_path: Path) -> None:
    def mono(q: str, d: Path) -> DownloadOutcome:
        raise RuntimeError("Turnstile JWT required")

    def ytdlp(q: str, d: Path) -> DownloadOutcome:
        return DownloadOutcome(Path("/x/a.opus"), 3, "ytdlp", 1.0, q)

    # no Tier-2 pool registered -> cascade goes Tier 1 -> Tier 3
    tiers = [_tier("monochrome", Tier.ONE, mono, True), _tier("ytdlp", Tier.THREE, ytdlp, False)]
    result = run_cascade("q", tmp_path, tiers, prober=lambda p: _opus())

    assert result.winner is not None and result.winner.outcome.source == "ytdlp"
    assert [(a.source, a.ok) for a in result.attempts] == [("monochrome", False), ("ytdlp", True)]


def test_tiers_evaluated_in_priority_order_regardless_of_input_order(tmp_path: Path) -> None:
    def t3(q: str, d: Path) -> DownloadOutcome:
        return DownloadOutcome(Path("/x/a.opus"), 3, "ytdlp", 1.0, q)

    def t1(q: str, d: Path) -> DownloadOutcome:
        return DownloadOutcome(Path("/x/a.flac"), 1, "monochrome", 1.0, q)

    # pass tiers out of order; cascade must still try Tier 1 first
    tiers = [_tier("ytdlp", Tier.THREE, t3, False), _tier("monochrome", Tier.ONE, t1, True)]
    result = run_cascade("q", tmp_path, tiers, prober=lambda p: _flac())
    assert result.winner is not None and result.winner.outcome.tier == 1


def test_no_winner_when_all_tiers_fail(tmp_path: Path) -> None:
    def boom(q: str, d: Path) -> DownloadOutcome:
        raise RuntimeError("down")

    tiers = [_tier("monochrome", Tier.ONE, boom, True), _tier("ytdlp", Tier.THREE, boom, False)]
    result = run_cascade("q", tmp_path, tiers, prober=lambda p: _flac())
    assert result.winner is None
    assert len(result.attempts) == 2 and all(not a.ok for a in result.attempts)
